// programador-sync: lector IMAP de iCloud que corre DENTRO de Supabase.
//
// Por que existe: las sesiones de Claude en la nube no pueden hablar IMAP, y el
// plan "el Mac sincroniza y publica" murio porque exigia que Lester hiciera
// algo en su Mac. Esta Edge Function si puede abrir TLS crudo contra
// imap.mail.me.com:993 (comprobado el 26 sep 2026), asi que el buzon se lee
// desde aqui, cada 10 minutos, disparado por pg_cron + pg_net, y se publica
// con la misma RPC `programador_publicar` que ya estaba probada en vivo.
//
// Secretos: la contrasena de app de Apple y el token del cron viven en Vault y
// se leen por la RPC `programador_secreto`, que solo puede ejecutar
// service_role. Aqui no hay nada hardcodeado.
//
// Autenticacion de esta funcion: cabecera `x-programador-token` igual al
// secreto `programador_cron_token` de Vault. verify_jwt esta apagado a
// proposito porque pg_net no manda JWT.

import "jsr:@supabase/functions-js/edge-runtime.d.ts";

const SUPABASE_URL = Deno.env.get("SUPABASE_URL") ?? "";
const SERVICE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "";

const IMAP_HOST = "imap.mail.me.com";
const IMAP_PORT = 993;
const FOLDERS = ["INBOX", "Sent Messages"];
const WINDOW_DAYS_DEFAULT = 3;
const WINDOW_DAYS_MAX = 14;
const RECON_EXTRA_DAYS = 2;
const MAX_NEW_PER_RUN = 120;
const FETCH_CHUNK = 25;
const PLAIN_FETCH_BYTES = 16000; // bytes crudos que se piden de una parte text/plain
const HTML_FETCH_BYTES = 64000; // el HTML de newsletter arrastra mucho <head>/<style>; hace falta mas
const MIN_BODY_QUALITY = 200; // caracteres visibles a partir de los cuales el text/plain se da por bueno
const BODY_LIMIT = 4000;
const SNIPPET_LIMIT = 240;
const PUBLISH_BATCH = 100;
const HOST_LABEL = "supabase-edge";

// Pistas documentales (mismas que config/entities.example.yaml).
const DOC_TYPE_HINTS: Record<string, string[]> = {
  invoice: ["factura", "invoice", "factura comercial", "commercial invoice"],
  proforma: ["proforma", "pro forma", "cotizacion", "quotation", "presupuesto"],
  purchase_order: ["orden de compra", "purchase order", "pedido n"],
  bill_of_lading: ["conocimiento de embarque", "bill of lading", "b/l", "air waybill", "carta de porte"],
  packing_list: ["packing list", "lista de empaque", "lista de bultos"],
  customs: ["aduana", "despacho aduanero", "customs", "arancel", "partida arancelaria", "hs code"],
  payment: ["transferencia", "swift", "comprobante de pago", "wire transfer", "remesa"],
  contract: ["contrato", "agreement", "addendum", "memorando de entendimiento"],
  certificate: ["certificado de origen", "certificate of origin", "certificado de calidad"],
  shipping: ["embarque", "shipment", "contenedor", "container", "booking", "naviera"],
  tax: ["modelo 303", "modelo 347", "declaracion de iva", "agencia tributaria"],
};

// ---------------------------------------------------------------------------
// Supabase (PostgREST con service_role)
// ---------------------------------------------------------------------------

async function rest(path: string, init: RequestInit & { prefer?: string } = {}): Promise<unknown> {
  const headers: Record<string, string> = {
    apikey: SERVICE_KEY,
    Authorization: `Bearer ${SERVICE_KEY}`,
    "Content-Type": "application/json",
  };
  if (init.prefer) headers["Prefer"] = init.prefer;
  const res = await fetch(`${SUPABASE_URL}/rest/v1/${path}`, { ...init, headers });
  const text = await res.text();
  if (!res.ok) throw new Error(`PostgREST ${res.status} en ${path.split("?")[0]}: ${text.slice(0, 300)}`);
  return text ? JSON.parse(text) : null;
}

async function secreto(nombre: string): Promise<string | null> {
  const v = await rest("rpc/programador_secreto", { method: "POST", body: JSON.stringify({ nombre }) });
  return typeof v === "string" && v.length > 0 ? v : null;
}

async function publicar(mensajes: Row[], estado: Record<string, unknown> | null): Promise<number> {
  const n = await rest("rpc/programador_publicar", {
    method: "POST",
    body: JSON.stringify({ mensajes, estado }),
  });
  return typeof n === "number" ? n : 0;
}

// ---------------------------------------------------------------------------
// Texto: normalizacion, RFC 2047, quoted-printable, base64, HTML
// ---------------------------------------------------------------------------

function normalize(value: string | null | undefined): string {
  if (!value) return "";
  return value.normalize("NFKD").replace(/\p{M}+/gu, "").toLowerCase().split(/\s+/).filter(Boolean).join(" ");
}

function decodeBytes(bytes: Uint8Array, charset: string): string {
  const label = (charset || "utf-8").toLowerCase().replace(/^"|"$/g, "");
  const candidates = [label, "utf-8", "windows-1252"];
  for (const c of candidates) {
    try {
      return new TextDecoder(c, { fatal: c === "utf-8" }).decode(bytes);
    } catch {
      // siguiente candidato
    }
  }
  return new TextDecoder("windows-1252").decode(bytes);
}

function base64ToBytes(b64: string): Uint8Array {
  let clean = b64.replace(/[^A-Za-z0-9+/=]/g, "");
  clean = clean.slice(0, clean.length - (clean.length % 4)); // un corte parcial deja un cuarteto a medias
  try {
    const bin = atob(clean);
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  } catch {
    return new Uint8Array(0);
  }
}

function qpToBytes(text: string, underscoreIsSpace = false): Uint8Array {
  const s = text.replace(/=\r?\n/g, "").replace(/=[0-9A-Fa-f]?$/, "");
  const out: number[] = [];
  for (let i = 0; i < s.length; i++) {
    const ch = s[i];
    if (ch === "=" && /^[0-9A-Fa-f]{2}$/.test(s.slice(i + 1, i + 3))) {
      out.push(parseInt(s.slice(i + 1, i + 3), 16));
      i += 2;
    } else if (ch === "_" && underscoreIsSpace) {
      out.push(32);
    } else {
      out.push(ch.charCodeAt(0) & 0xff);
    }
  }
  return new Uint8Array(out);
}

function decodeRfc2047(value: string): string {
  if (!value || !value.includes("=?")) return value;
  const joined = value.replace(/\?=\s+=\?/g, "?==?");
  return joined.replace(/=\?([^?]+)\?([bBqQ])\?([^?]*)\?=/g, (_m, cs: string, enc: string, data: string) => {
    const charset = cs.split("*")[0];
    const bytes = enc.toLowerCase() === "b" ? base64ToBytes(data) : qpToBytes(data, true);
    return decodeBytes(bytes, charset);
  });
}

function decodeRfc2231(value: string): string {
  // filename*=utf-8''hola%20mundo.pdf
  const m = /^([^']*)'[^']*'(.*)$/.exec(value);
  if (!m) return value;
  try {
    const pct = m[2];
    const bytes: number[] = [];
    for (let i = 0; i < pct.length; i++) {
      if (pct[i] === "%" && /^[0-9A-Fa-f]{2}$/.test(pct.slice(i + 1, i + 3))) {
        bytes.push(parseInt(pct.slice(i + 1, i + 3), 16));
        i += 2;
      } else bytes.push(pct.charCodeAt(i) & 0xff);
    }
    return decodeBytes(new Uint8Array(bytes), m[1] || "utf-8");
  } catch {
    return value;
  }
}

const HTML_ENTITIES: Record<string, string> = {
  amp: "&", lt: "<", gt: ">", quot: '"', apos: "'", nbsp: " ", aacute: "á", eacute: "é", iacute: "í",
  oacute: "ó", uacute: "ú", ntilde: "ñ", Aacute: "Á", Eacute: "É", Iacute: "Í", Oacute: "Ó", Uacute: "Ú",
  Ntilde: "Ñ", uuml: "ü", Uuml: "Ü", iquest: "¿", iexcl: "¡", euro: "€", copy: "©", reg: "®", hellip: "…",
  ndash: "–", mdash: "—", laquo: "«", raquo: "»", rsquo: "’", lsquo: "‘", ldquo: "“", rdquo: "”",
  zwnj: "", zwj: "", shy: "", thinsp: " ", ensp: " ", emsp: " ", bull: "•", middot: "·", trade: "™",
  deg: "°", times: "×", agrave: "à", egrave: "è", igrave: "ì", ograve: "ò", ugrave: "ù", ccedil: "ç",
  Ccedil: "Ç", ouml: "ö", auml: "ä", szlig: "ß", ordm: "º", ordf: "ª", pound: "£", dollar: "$",
};

// Caracteres invisibles que los newsletters usan como relleno del preheader.
const INVISIBLE_RE = /[\u00AD\u034F\u200B-\u200F\u2060\u2061-\u2064\uFEFF]/g;

function stripInvisible(text: string): string {
  return text.replace(INVISIBLE_RE, "");
}

// Heuristica: un text/plain que en realidad trae HTML/CSS (muy comun en newsletters).
function looksHtml(text: string): boolean {
  const head = text.slice(0, 4000);
  return /<!doctype\s|<html[\s>]|<body[\s>]|<table[\s>]|<div[\s>]|<td[\s>]|<p[\s>]|<br\s*\/?>|<style[\s>]|@import\s+url|<!--/i.test(head) ||
    (head.match(/&[a-zA-Z]{2,8};/g) ?? []).length >= 5;
}

// Caracteres "con contenido": sin espacios, invisibles ni signos de relleno.
function visibleLength(text: string): number {
  return stripInvisible(text).replace(/[\s\u00A0|_\-=*~.·•]+/g, "").length;
}

function htmlToText(html: string): string {
  // Bloques sin texto util; si el HTML viene truncado y no cierran, se descartan hasta el final.
  let s = html.replace(/<(script|style|head|title)\b[\s\S]*?(?:<\/\1\s*>|$)/gi, " ");
  s = s.replace(/<!--[\s\S]*?(?:-->|$)/g, " ");
  s = s.replace(/<br\s*\/?>/gi, "\n").replace(/<\/(p|div|tr|li|h[1-6]|table|blockquote)>/gi, "\n");
  s = s.replace(/<[^>]*>/g, " ");
  s = s.replace(/<[^>]*$/, " "); // etiqueta cortada por el truncado
  s = s.replace(/^[^<]*?>/, " "); // resto de etiqueta si el fragmento empieza a mitad
  // CSS suelto (text/plain que en realidad es la hoja de estilos del newsletter)
  s = s.replace(/\/\*[\s\S]*?(?:\*\/|$)/g, " ");
  s = s.replace(/@import\s+url\([^)]*\)\s*;?/gi, " ");
  s = s.replace(/@media[^{]*\{[\s\S]*?\}\s*\}/gi, " ");
  s = s.replace(/(?:^|\n)\s*[a-zA-Z.#*:\[\]="'\-,>\s]{1,120}\{[^{}]*\}/g, "\n");
  s = s.replace(/&(#x[0-9a-fA-F]+|#\d+|[a-zA-Z]+);/g, (m, e: string) => {
    if (e.startsWith("#x")) return String.fromCodePoint(parseInt(e.slice(2), 16));
    if (e.startsWith("#")) return String.fromCodePoint(parseInt(e.slice(1), 10));
    return HTML_ENTITIES[e] ?? m;
  });
  s = stripInvisible(s);
  return s.replace(/[ \t\r\f\v\u00A0]+/g, " ").replace(/\s*\n\s*/g, "\n").replace(/\n{3,}/g, "\n\n").trim();
}

function collapse(text: string): string {
  return text.replace(/\s+/g, " ").trim();
}

// ---------------------------------------------------------------------------
// Cabeceras y direcciones
// ---------------------------------------------------------------------------

function parseHeaders(raw: string): Record<string, string> {
  const unfolded = raw.replace(/\r?\n[ \t]+/g, " ");
  const out: Record<string, string> = {};
  for (const line of unfolded.split(/\r?\n/)) {
    const idx = line.indexOf(":");
    if (idx <= 0) continue;
    const name = line.slice(0, idx).trim().toLowerCase();
    const value = line.slice(idx + 1).trim();
    out[name] = out[name] ? `${out[name]}, ${value}` : value;
  }
  return out;
}

interface Addr { name: string; addr: string }

function parseAddresses(value: string): Addr[] {
  if (!value) return [];
  const decoded = decodeRfc2047(value);
  const out: Addr[] = [];
  const re = /(?:"([^"]*)"|([^<,"]*?))?\s*<([^<>]+)>|([^\s,<>"]+@[^\s,<>"]+)/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(decoded)) !== null) {
    const addr = (m[3] ?? m[4] ?? "").trim().toLowerCase();
    if (!addr) continue;
    const name = (m[1] ?? m[2] ?? "").trim().replace(/^'|'$/g, "");
    out.push({ name, addr });
  }
  return out;
}

const MONTHS: Record<string, number> = {
  jan: 0, feb: 1, mar: 2, apr: 3, may: 4, jun: 5, jul: 6, aug: 7, sep: 8, oct: 9, nov: 10, dec: 11,
};

function parseInternalDate(s: string): Date | null {
  // 26-Sep-2026 10:11:12 +0000
  const m = /^\s*(\d{1,2})-([A-Za-z]{3})-(\d{4}) (\d{2}):(\d{2}):(\d{2}) ([+-])(\d{2})(\d{2})/.exec(s);
  if (!m) return null;
  const mon = MONTHS[m[2].toLowerCase()];
  if (mon === undefined) return null;
  const sign = m[7] === "-" ? -1 : 1;
  const offsetMin = sign * (parseInt(m[8]) * 60 + parseInt(m[9]));
  const utc = Date.UTC(parseInt(m[3]), mon, parseInt(m[1]), parseInt(m[4]), parseInt(m[5]), parseInt(m[6]));
  return new Date(utc - offsetMin * 60000);
}

function parseDateHeader(value: string | undefined, internal: string | undefined): Date | null {
  if (value) {
    const cleaned = value.replace(/\s*\([^)]*\)\s*$/, "");
    const d = new Date(cleaned);
    if (!isNaN(d.getTime())) return d;
  }
  return internal ? parseInternalDate(internal) : null;
}

function imapDate(d: Date): string {
  const names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  return `${d.getUTCDate()}-${names[d.getUTCMonth()]}-${d.getUTCFullYear()}`;
}

// ---------------------------------------------------------------------------
// Cliente IMAP minimo sobre Deno.connectTls
// ---------------------------------------------------------------------------

interface Untagged { text: string; literals: Uint8Array[] }
interface Reply { ok: boolean; status: string; untagged: Untagged[] }

type Tok = string | Tok[] | { lit: number } | null;

class ImapError extends Error {}

class Imap {
  private conn!: Deno.TlsConn;
  private buf = new Uint8Array(0);
  private tagN = 0;
  private latin1 = new TextDecoder("latin1");
  private enc = new TextEncoder();

  async connect(): Promise<string> {
    this.conn = await Deno.connectTls({ hostname: IMAP_HOST, port: IMAP_PORT });
    return await this.readLine();
  }

  close(): void {
    try { this.conn.close(); } catch { /* ya cerrada */ }
  }

  private async fill(): Promise<void> {
    const chunk = new Uint8Array(65536);
    const n = await this.conn.read(chunk);
    if (n === null) throw new ImapError("IMAP: el servidor cerro la conexion");
    const nb = new Uint8Array(this.buf.length + n);
    nb.set(this.buf);
    nb.set(chunk.subarray(0, n), this.buf.length);
    this.buf = nb;
  }

  private async readLine(): Promise<string> {
    for (;;) {
      for (let i = 0; i + 1 < this.buf.length; i++) {
        if (this.buf[i] === 13 && this.buf[i + 1] === 10) {
          const line = this.latin1.decode(this.buf.subarray(0, i));
          this.buf = this.buf.slice(i + 2);
          return line;
        }
      }
      await this.fill();
    }
  }

  private async readBytes(n: number): Promise<Uint8Array> {
    while (this.buf.length < n) await this.fill();
    const out = this.buf.slice(0, n);
    this.buf = this.buf.slice(n);
    return out;
  }

  private async writeAll(bytes: Uint8Array): Promise<void> {
    let off = 0;
    while (off < bytes.length) off += await this.conn.write(bytes.subarray(off));
  }

  async command(cmd: string): Promise<Reply> {
    const tag = "A" + String(++this.tagN).padStart(4, "0");
    await this.writeAll(this.enc.encode(`${tag} ${cmd}\r\n`));
    const untagged: Untagged[] = [];
    for (;;) {
      let line = await this.readLine();
      if (line.startsWith(tag + " ")) {
        const status = line.slice(tag.length + 1);
        return { ok: status.startsWith("OK"), status, untagged };
      }
      if (line.startsWith("+")) continue;
      const literals: Uint8Array[] = [];
      let text = "";
      for (;;) {
        const m = /\{(\d+)\}$/.exec(line);
        if (!m) { text += line; break; }
        text += line.slice(0, m.index) + `\u0001${literals.length}\u0001`;
        literals.push(await this.readBytes(parseInt(m[1])));
        line = await this.readLine();
      }
      untagged.push({ text, literals });
    }
  }

  async must(cmd: string, what: string): Promise<Reply> {
    const r = await this.command(cmd);
    if (!r.ok) throw new ImapError(`${what}: ${r.status.slice(0, 200)}`);
    return r;
  }
}

function quote(s: string): string {
  return `"${s.replace(/[\\"]/g, (c) => "\\" + c)}"`;
}

// Tokenizador de respuestas IMAP (atomos, cadenas, listas, literales).
function tokenize(text: string): Tok[] {
  let i = 0;
  const n = text.length;
  function skipWs() { while (i < n && text[i] === " ") i++; }
  function readList(): Tok[] {
    const out: Tok[] = [];
    for (;;) {
      skipWs();
      if (i >= n) return out;
      const ch = text[i];
      if (ch === ")") { i++; return out; }
      if (ch === "(") { i++; out.push(readList()); continue; }
      if (ch === '"') {
        i++;
        let s = "";
        while (i < n && text[i] !== '"') {
          if (text[i] === "\\" && i + 1 < n) { s += text[i + 1]; i += 2; } else { s += text[i]; i++; }
        }
        i++;
        out.push(s);
        continue;
      }
      if (ch === "\u0001") {
        const end = text.indexOf("\u0001", i + 1);
        out.push({ lit: parseInt(text.slice(i + 1, end)) });
        i = end + 1;
        continue;
      }
      // atomo; los corchetes (BODY[HEADER.FIELDS (FROM TO)]<0>) forman parte del atomo
      let s = "";
      let depth = 0;
      while (i < n) {
        const c = text[i];
        if (c === "[") depth++;
        if (c === "]") depth--;
        if (depth === 0 && (c === " " || c === ")" || c === "(" || c === "\u0001")) break;
        s += c;
        i++;
      }
      out.push(s === "NIL" ? null : s);
    }
  }
  return readList();
}

function str(t: Tok): string {
  return typeof t === "string" ? t : "";
}

function paramMap(t: Tok): Map<string, string> {
  const m = new Map<string, string>();
  if (!Array.isArray(t)) return m;
  for (let i = 0; i + 1 < t.length; i += 2) {
    const k = str(t[i]).toLowerCase();
    let v = str(t[i + 1]);
    if (k.endsWith("*")) { m.set(k.slice(0, -1), decodeRfc2231(v)); continue; }
    if (!k) continue;
    v = decodeRfc2047(v);
    if (!m.has(k)) m.set(k, v);
  }
  return m;
}

interface Part {
  section: string;
  type: string;
  subtype: string;
  charset: string;
  encoding: string;
  filename: string | null;
  disposition: string | null;
  size: number;
}

function walkStructure(node: Tok[], section: string | null, out: Part[]): void {
  if (Array.isArray(node[0])) {
    let i = 0;
    const kids: Tok[][] = [];
    while (i < node.length && Array.isArray(node[i])) kids.push(node[i++] as Tok[]);
    kids.forEach((k, idx) => walkStructure(k, section ? `${section}.${idx + 1}` : String(idx + 1), out));
    return;
  }
  const type = str(node[0]).toLowerCase();
  const subtype = str(node[1]).toLowerCase();
  const params = paramMap(node[2]);
  const encoding = str(node[5]).toLowerCase();
  const size = parseInt(str(node[6])) || 0;
  let disposition: string | null = null;
  let filename = params.get("name") ?? null;
  for (let i = 7; i < node.length; i++) {
    const t = node[i];
    if (Array.isArray(t) && typeof t[0] === "string" && Array.isArray(t[1])) {
      const d = t[0].toLowerCase();
      if (d !== "attachment" && d !== "inline") continue;
      disposition = d;
      const dm = paramMap(t[1]);
      if (dm.get("filename")) filename = dm.get("filename") ?? filename;
      break;
    }
  }
  out.push({ section: section ?? "1", type, subtype, charset: params.get("charset") ?? "utf-8", encoding, filename, disposition, size });
  // message/rfc822 anidado: su estructura va tras el envelope
  if (type === "message" && subtype === "rfc822") {
    for (let i = 7; i < node.length; i++) {
      const t = node[i];
      if (Array.isArray(t) && (Array.isArray(t[0]) || (typeof t[0] === "string" && typeof t[1] === "string" && Array.isArray(t[2])))) {
        walkStructure(t, section ? `${section}` : "1", out);
        break;
      }
    }
  }
}

function fetchItems(list: Tok[]): Map<string, Tok> {
  const m = new Map<string, Tok>();
  for (let i = 0; i + 1 < list.length; i += 2) {
    const k = str(list[i]).toUpperCase().replace(/<\d+>$/, "");
    m.set(k, list[i + 1]);
  }
  return m;
}

function litText(t: Tok, literals: Uint8Array[]): Uint8Array | null {
  if (t && typeof t === "object" && !Array.isArray(t) && "lit" in t) return literals[t.lit] ?? null;
  if (typeof t === "string") return new TextEncoder().encode(t);
  return null;
}

function decodeSmart(bytes: Uint8Array): string {
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    return new TextDecoder("windows-1252").decode(bytes);
  }
}

function decodeBody(bytes: Uint8Array, part: Part): string {
  let raw: Uint8Array;
  const enc = part.encoding;
  if (enc === "base64") raw = base64ToBytes(new TextDecoder("latin1").decode(bytes));
  else if (enc === "quoted-printable") raw = qpToBytes(new TextDecoder("latin1").decode(bytes));
  else raw = bytes;
  let text = decodeBytes(raw, part.charset);
  if (part.subtype === "html" || looksHtml(text)) text = htmlToText(text);
  else text = stripInvisible(text);
  return text.replace(/\r\n/g, "\n").trim();
}

// Pide una seccion MIME (los primeros `bytes`) y devuelve sus bytes crudos.
async function fetchSection(imap: Imap, uid: number, section: string, bytes: number): Promise<Uint8Array | null> {
  const br = await imap.command(`UID FETCH ${uid} (BODY.PEEK[${section}]<0.${bytes}>)`);
  if (!br.ok) return null;
  for (const bu of br.untagged) {
    if (!/^\* \d+ FETCH /.test(bu.text)) continue;
    const bl = tokenize(bu.text)[3];
    if (!Array.isArray(bl)) continue;
    const bi = fetchItems(bl);
    const b = litText(bi.get(`BODY[${section.toUpperCase()}]`) ?? bi.get(`BODY[${section}]`) ?? null, bu.literals);
    if (b) return b;
  }
  return null;
}

// Cuerpo legible de un mensaje: une los text/plain (un multipart/mixed puede
// traer varios, p.ej. texto-PDF-texto) y, si lo que queda es pobre o vacio y
// hay text/html, convierte el HTML. Devuelve el texto y si quedo truncado.
async function readBody(imap: Imap, uid: number, parts: Part[]): Promise<{ text: string; truncado: boolean }> {
  const textParts = parts.filter((p) => p.type === "text" && p.disposition !== "attachment");
  const plains = textParts.filter((p) => p.subtype === "plain");
  const html = textParts.find((p) => p.subtype === "html") ?? null;
  let text = "";
  let truncado = false;
  const chunks: string[] = [];
  for (const p of plains.slice(0, 4)) {
    if (p.size <= 8) continue; // "\r\n" o equivalente: parte vacia, no merece un viaje
    const bytes = await fetchSection(imap, uid, p.section, PLAIN_FETCH_BYTES);
    if (!bytes) continue;
    const t = decodeBody(bytes, p);
    if (t) chunks.push(t);
    if (p.size > PLAIN_FETCH_BYTES) truncado = true;
  }
  text = chunks.join("\n\n").trim();
  if (html && visibleLength(text) < MIN_BODY_QUALITY) {
    const bytes = await fetchSection(imap, uid, html.section, HTML_FETCH_BYTES);
    if (bytes) {
      const t = decodeBody(bytes, html);
      if (visibleLength(t) > visibleLength(text)) {
        text = t;
        truncado = html.size > HTML_FETCH_BYTES;
      }
    }
  }
  return { text, truncado };
}

// ---------------------------------------------------------------------------
// Enrutado
// ---------------------------------------------------------------------------

interface Rule {
  entity: string;
  priority: number;
  from_domain: string[];
  from_addr: string[];
  to_addr: string[];
  subject_contains: string[];
  body_contains: string[];
}

interface Row {
  key: string;
  account: string;
  folder: string;
  uid: number;
  uidvalidity: number;
  message_id: string | null;
  from_addr: string;
  from_name: string;
  to_addrs: string[];
  cc_addrs: string[];
  subject: string;
  date_utc: string | null;
  snippet: string;
  body_text: string;
  body_truncado: boolean;
  attachments: string[];
  flags: string[];
  entity: string;
  routing_reason: string;
  doc_types: string[];
}

function route(rules: Rule[], row: Row): { entity: string; reason: string } {
  const fromN = normalize(row.from_addr);
  const domainN = fromN.includes("@") ? fromN.split("@")[1] : "";
  const recipients = new Set([...row.to_addrs, ...row.cc_addrs].map(normalize));
  const subjectN = normalize(row.subject);
  const bodyN = normalize(`${row.body_text} ${row.attachments.join(" ")}`);
  for (const r of rules) {
    for (const d of r.from_domain ?? []) {
      const dn = normalize(d).replace(/^@/, "");
      if (domainN && domainN === dn) return { entity: r.entity, reason: `from_domain=${dn}` };
    }
    for (const a of r.from_addr ?? []) {
      const an = normalize(a);
      if (fromN && fromN === an) return { entity: r.entity, reason: `from_addr=${an}` };
    }
    for (const a of r.to_addr ?? []) {
      const an = normalize(a);
      if (recipients.has(an)) return { entity: r.entity, reason: `to_addr=${an}` };
    }
    for (const s of r.subject_contains ?? []) {
      const sn = normalize(s);
      if (sn && subjectN.includes(sn)) return { entity: r.entity, reason: `subject_contains=${sn}` };
    }
    for (const s of r.body_contains ?? []) {
      const sn = normalize(s);
      if (sn && bodyN.includes(sn)) return { entity: r.entity, reason: `body_contains=${sn}` };
    }
  }
  return { entity: "personal", reason: "default" };
}

function docTypes(row: Row): string[] {
  const hay = `${normalize(row.subject)} ${normalize(row.body_text)} ${normalize(row.attachments.join(" "))}`;
  const found: string[] = [];
  for (const [t, kws] of Object.entries(DOC_TYPE_HINTS)) {
    if (kws.some((k) => hay.includes(normalize(k)))) found.push(t);
  }
  return found.sort();
}

// ---------------------------------------------------------------------------
// Sincronizacion de una carpeta
// ---------------------------------------------------------------------------

interface FolderReport {
  folder: string;
  uidvalidity: number;
  reset: boolean;
  en_ventana: number;
  nuevos: number;
  publicados: number;
  flags_actualizados: number;
  borrados: number;
  pendientes: number;
  error?: string;
}

interface StateRow { account: string; folder: string; uidvalidity: number; last_uid: number }

function uidSet(uids: number[]): string {
  // rangos compactos: 1:5,8,10:12
  const s = [...uids].sort((a, b) => a - b);
  const parts: string[] = [];
  let i = 0;
  while (i < s.length) {
    let j = i;
    while (j + 1 < s.length && s[j + 1] === s[j] + 1) j++;
    parts.push(i === j ? String(s[i]) : `${s[i]}:${s[j]}`);
    i = j + 1;
  }
  return parts.join(",");
}

function flagsOf(t: Tok): string[] {
  return Array.isArray(t) ? t.map(str).filter(Boolean) : [];
}

function sameFlags(a: string[], b: string[]): boolean {
  const sa = [...a].sort().join(" ");
  const sb = [...b].sort().join(" ");
  return sa === sb;
}

async function syncFolder(imap: Imap, account: string, folder: string, rules: Rule[], windowDays: number): Promise<FolderReport> {
  const rep: FolderReport = {
    folder, uidvalidity: 0, reset: false, en_ventana: 0, nuevos: 0, publicados: 0,
    flags_actualizados: 0, borrados: 0, pendientes: 0,
  };
  const sel = await imap.must(`SELECT ${quote(folder)}`, `SELECT ${folder}`);
  let uidvalidity = 0;
  for (const u of sel.untagged) {
    const m = /\[UIDVALIDITY (\d+)\]/.exec(u.text);
    if (m) uidvalidity = parseInt(m[1]);
  }
  if (!uidvalidity) throw new ImapError(`${folder}: el servidor no devolvio UIDVALIDITY`);
  rep.uidvalidity = uidvalidity;

  const stateRows = (await rest(
    `programador_sync_state?account=eq.${encodeURIComponent(account)}&folder=eq.${encodeURIComponent(folder)}&select=account,folder,uidvalidity,last_uid`,
  )) as StateRow[];
  const state = stateRows?.[0] ?? null;
  const reset = !state || Number(state.uidvalidity) !== uidvalidity;
  rep.reset = reset;
  const lastUid = reset ? 0 : Number(state!.last_uid);

  if (state && reset) {
    // La carpeta se reconstruyo en el servidor: todo lo publicado de ella es basura.
    await rest(
      `programador_mensajes?account=eq.${encodeURIComponent(account)}&folder=eq.${encodeURIComponent(folder)}`,
      { method: "DELETE" },
    );
  }

  const since = new Date(Date.now() - (windowDays + RECON_EXTRA_DAYS) * 86400000);
  const search = await imap.must(`UID SEARCH SINCE ${imapDate(since)}`, `SEARCH ${folder}`);
  const windowUids: number[] = [];
  for (const u of search.untagged) {
    const m = /^\* SEARCH\s*(.*)$/.exec(u.text);
    if (m && m[1].trim()) windowUids.push(...m[1].trim().split(/\s+/).map((x) => parseInt(x)).filter((x) => x > 0));
  }
  rep.en_ventana = windowUids.length;
  const windowSet = new Set(windowUids);

  let newUids = windowUids.filter((u) => u > lastUid).sort((a, b) => a - b);
  rep.pendientes = Math.max(0, newUids.length - MAX_NEW_PER_RUN);
  newUids = newUids.slice(0, MAX_NEW_PER_RUN);
  rep.nuevos = newUids.length;

  // 1) Mensajes nuevos: cabeceras + estructura, luego el cuerpo de texto.
  const rows: Row[] = [];
  for (let i = 0; i < newUids.length; i += FETCH_CHUNK) {
    const chunk = newUids.slice(i, i + FETCH_CHUNK);
    const r = await imap.must(
      `UID FETCH ${uidSet(chunk)} (UID FLAGS INTERNALDATE BODYSTRUCTURE BODY.PEEK[HEADER.FIELDS (FROM TO CC SUBJECT DATE MESSAGE-ID)])`,
      `FETCH cabeceras ${folder}`,
    );
    for (const u of r.untagged) {
      if (!/^\* \d+ FETCH /.test(u.text)) continue;
      const toks = tokenize(u.text);
      const list = toks[3];
      if (!Array.isArray(list)) continue;
      const items = fetchItems(list);
      const uid = parseInt(str(items.get("UID")));
      if (!uid) continue;
      const headerBytes = litText(items.get("BODY[HEADER.FIELDS (FROM TO CC SUBJECT DATE MESSAGE-ID)]") ?? null, u.literals);
      const headers = parseHeaders(headerBytes ? decodeSmart(headerBytes) : "");
      const parts: Part[] = [];
      const bs = items.get("BODYSTRUCTURE");
      if (Array.isArray(bs)) walkStructure(bs, null, parts);

      const from = parseAddresses(headers["from"] ?? "")[0] ?? { name: "", addr: "" };
      const to = parseAddresses(headers["to"] ?? "").map((a) => a.addr);
      const cc = parseAddresses(headers["cc"] ?? "").map((a) => a.addr);
      const date = parseDateHeader(headers["date"], str(items.get("INTERNALDATE")));

      const textSections = new Set(parts.filter((p) => p.type === "text" && p.disposition !== "attachment").map((p) => p.section));
      const attachments = parts
        .filter((p) => !textSections.has(p.section) && (p.disposition === "attachment" || (p.filename && p.type !== "text")))
        .map((p) => p.filename ?? `${p.type}/${p.subtype}`)
        .filter((v, idx, arr) => arr.indexOf(v) === idx);

      let { text: body, truncado } = await readBody(imap, uid, parts);
      if (body.length > BODY_LIMIT) { body = body.slice(0, BODY_LIMIT); truncado = true; }

      const row: Row = {
        key: `${account}:${folder}:${uidvalidity}:${uid}`,
        account, folder, uid, uidvalidity,
        message_id: headers["message-id"]?.trim() || null,
        from_addr: from.addr,
        from_name: from.name,
        to_addrs: to,
        cc_addrs: cc,
        subject: decodeRfc2047(headers["subject"] ?? "").trim(),
        date_utc: date ? date.toISOString() : null,
        snippet: collapse(body).slice(0, SNIPPET_LIMIT),
        body_text: body,
        body_truncado: truncado,
        attachments,
        flags: flagsOf(items.get("FLAGS")),
        entity: "personal",
        routing_reason: "default",
        doc_types: [],
      };
      const rt = route(rules, row);
      row.entity = rt.entity;
      row.routing_reason = rt.reason;
      row.doc_types = docTypes(row);
      rows.push(row);
    }
  }

  for (let i = 0; i < rows.length; i += PUBLISH_BATCH) {
    rep.publicados += await publicar(rows.slice(i, i + PUBLISH_BATCH), null);
  }

  // 2) Reconciliacion de lo ya publicado en la ventana: flags que cambian y
  //    mensajes que desaparecieron (movidos o borrados en iCloud).
  const existing = (await rest(
    `programador_mensajes?account=eq.${encodeURIComponent(account)}&folder=eq.${encodeURIComponent(folder)}&uidvalidity=eq.${uidvalidity}&date_utc=gte.${encodeURIComponent(new Date(Date.now() - windowDays * 86400000).toISOString())}&select=key,uid,flags`,
  )) as { key: string; uid: number; flags: string[] }[];
  const newSet = new Set(newUids);
  const toRefresh = existing.filter((e) => windowSet.has(Number(e.uid)) && !newSet.has(Number(e.uid)));
  const toDelete = existing.filter((e) => !windowSet.has(Number(e.uid)) && !newSet.has(Number(e.uid)));

  if (toRefresh.length) {
    const byUid = new Map(toRefresh.map((e) => [Number(e.uid), e]));
    for (let i = 0; i < toRefresh.length; i += 200) {
      const chunk = toRefresh.slice(i, i + 200).map((e) => Number(e.uid));
      const fr = await imap.command(`UID FETCH ${uidSet(chunk)} (UID FLAGS)`);
      if (!fr.ok) continue;
      for (const u of fr.untagged) {
        if (!/^\* \d+ FETCH /.test(u.text)) continue;
        const l = tokenize(u.text)[3];
        if (!Array.isArray(l)) continue;
        const it = fetchItems(l);
        const uid = parseInt(str(it.get("UID")));
        const e = byUid.get(uid);
        if (!e) continue;
        const flags = flagsOf(it.get("FLAGS"));
        if (!sameFlags(flags, e.flags ?? [])) {
          await rest(`programador_mensajes?key=eq.${encodeURIComponent(e.key)}`, {
            method: "PATCH",
            body: JSON.stringify({ flags }),
          });
          rep.flags_actualizados++;
        }
      }
    }
  }
  if (toDelete.length) {
    const keys = toDelete.map((e) => `"${e.key.replace(/"/g, '\\"')}"`).join(",");
    await rest(`programador_mensajes?key=in.(${encodeURIComponent(keys)})`, { method: "DELETE" });
    rep.borrados = toDelete.length;
  }

  // 3) Estado incremental.
  const maxNew = newUids.length ? newUids[newUids.length - 1] : lastUid;
  await rest("programador_sync_state", {
    method: "POST",
    prefer: "resolution=merge-duplicates",
    body: JSON.stringify({ account, folder, uidvalidity, last_uid: Math.max(lastUid, maxNew), updated_at: new Date().toISOString() }),
  });
  return rep;
}

// ---------------------------------------------------------------------------
// Punto de entrada
// ---------------------------------------------------------------------------

Deno.serve(async (req: Request) => {
  const started = Date.now();
  const json = (body: unknown, status = 200) =>
    new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

  if (!SUPABASE_URL || !SERVICE_KEY) return json({ error: "faltan SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY" }, 500);

  const token = await secreto("programador_cron_token");
  if (!token || req.headers.get("x-programador-token") !== token) return json({ error: "no autorizado" }, 401);

  let windowDays = WINDOW_DAYS_DEFAULT;
  let onlyFolders: string[] | null = null;
  try {
    const body = req.method === "POST" ? await req.json().catch(() => ({})) : {};
    if (body && typeof body.dias === "number") windowDays = Math.min(WINDOW_DAYS_MAX, Math.max(1, Math.floor(body.dias)));
    if (body && Array.isArray(body.carpetas)) onlyFolders = body.carpetas.filter((f: unknown) => typeof f === "string");
  } catch { /* cuerpo vacio */ }

  const account = (await secreto("icloud_email")) ?? "";
  const password = await secreto("icloud_app_password");
  if (!account || !password) {
    await publicar([], { account: account || "?", total_mensajes: 0, publicados: 0, ventana_dias: windowDays, host: HOST_LABEL, error: "Faltan los secretos icloud_email / icloud_app_password en Vault" }).catch(() => 0);
    return json({ error: "Faltan los secretos icloud_email / icloud_app_password en Vault" }, 500);
  }

  const rules = ((await rest("programador_reglas?enabled=eq.true&order=priority.desc,id.asc&select=entity,priority,from_domain,from_addr,to_addr,subject_contains,body_contains").catch(() => [])) ?? []) as Rule[];

  const reports: FolderReport[] = [];
  let fatal: string | null = null;
  const imap = new Imap();
  try {
    await imap.connect();
    await imap.must(`LOGIN ${quote(account)} ${quote(password)}`, "LOGIN en iCloud");
    for (const folder of onlyFolders ?? FOLDERS) {
      try {
        reports.push(await syncFolder(imap, account, folder, rules, windowDays));
      } catch (e) {
        reports.push({ folder, uidvalidity: 0, reset: false, en_ventana: 0, nuevos: 0, publicados: 0, flags_actualizados: 0, borrados: 0, pendientes: 0, error: String(e).slice(0, 300) });
      }
    }
    await imap.command("LOGOUT").catch(() => null);
  } catch (e) {
    fatal = String(e).slice(0, 400);
  } finally {
    imap.close();
  }

  const publicados = reports.reduce((a, r) => a + r.publicados, 0);
  const enVentana = reports.reduce((a, r) => a + r.en_ventana, 0);
  const folderErrors = reports.filter((r) => r.error).map((r) => `${r.folder}: ${r.error}`);
  const error = fatal ?? (folderErrors.length ? folderErrors.join(" | ") : null);
  try {
    await publicar([], {
      account, synced_at: new Date().toISOString(), total_mensajes: enVentana, publicados,
      ventana_dias: windowDays, host: HOST_LABEL, error,
    });
  } catch (e) {
    return json({ ok: false, error: `No se pudo escribir el sync_log: ${String(e).slice(0, 200)}`, reports, ms: Date.now() - started }, 500);
  }
  return json({ ok: !error, error, publicados, en_ventana: enVentana, reports, ms: Date.now() - started }, error ? 500 : 200);
});
