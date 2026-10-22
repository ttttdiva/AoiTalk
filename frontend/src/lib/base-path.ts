const ABSOLUTE_URL_RE = /^[a-z][a-z0-9+.-]*:/iu;

export function normalizeBasePath(value: string | null | undefined): string {
  const raw = String(value ?? "").trim();
  if (!raw || raw === "/") return "";
  if (ABSOLUTE_URL_RE.test(raw) || raw.startsWith("//") || raw.includes("?") || raw.includes("#")) {
    throw new Error("Public base path must be an origin-relative pathname");
  }
  const normalized = `/${raw.replace(/^\/+|\/+$/gu, "")}`;
  if (!/^\/[A-Za-z0-9._~-]+(?:\/[A-Za-z0-9._~-]+)*$/u.test(normalized)) {
    throw new Error(`Invalid public base path: ${raw}`);
  }
  return normalized;
}

export const PUBLIC_BASE_PATH = normalizeBasePath(
  process.env.NEXT_PUBLIC_BASE_PATH,
);

export function withBasePath(value: string): string {
  if (!PUBLIC_BASE_PATH || !value || value.startsWith("#") || value.startsWith("?")) return value;
  if (ABSOLUTE_URL_RE.test(value) || value.startsWith("//")) return value;
  if (!value.startsWith("/")) return value;
  if (value === PUBLIC_BASE_PATH || value.startsWith(`${PUBLIC_BASE_PATH}/`)) return value;
  return `${PUBLIC_BASE_PATH}${value}`;
}

export function withConfiguredBasePath(value: string, basePath: string): string {
  const normalized = normalizeBasePath(basePath);
  if (!normalized || !value || value.startsWith("#") || value.startsWith("?")) return value;
  if (ABSOLUTE_URL_RE.test(value) || value.startsWith("//") || !value.startsWith("/")) return value;
  if (value === normalized || value.startsWith(`${normalized}/`)) return value;
  return `${normalized}${value}`;
}

export function withoutBasePath(value: string): string {
  if (!PUBLIC_BASE_PATH) return value;
  if (value === PUBLIC_BASE_PATH) return "/";
  if (value.startsWith(`${PUBLIC_BASE_PATH}/`)) {
    return value.slice(PUBLIC_BASE_PATH.length) || "/";
  }
  return value;
}

export function publicCookiePath(): string {
  return PUBLIC_BASE_PATH || "/";
}

export function withBasePathOnSameOrigin(value: string, origin: string): string {
  if (!PUBLIC_BASE_PATH || !value) return value;
  if (value.startsWith("/") && !value.startsWith("//")) return withBasePath(value);
  try {
    const url = new URL(value, origin);
    if (url.origin !== origin) return value;
    url.pathname = withBasePath(url.pathname);
    return url.toString();
  } catch {
    return value;
  }
}

export function browserBasePathBootstrap(basePath = PUBLIC_BASE_PATH): string {
  const normalized = normalizeBasePath(basePath);
  if (!normalized) return "";
  const encoded = JSON.stringify(normalized);
  return `(function(base){
if(!base||window.__atBasePathInstalled)return;
window.__atBasePathInstalled=true;
function add(value){
  if(typeof value!=="string"||!value)return value;
  if(value[0]==="/"&&value[1]!=="/"){
    if(value===base||value.indexOf(base+"/")===0)return value;
    return base+value;
  }
  try{var url=new URL(value,window.location.origin);if(url.origin!==window.location.origin)return value;url.pathname=add(url.pathname);return url.href;}catch(_){return value;}
}
window.__atWithBasePath=add;
var originalFetch=window.fetch.bind(window);
window.fetch=function(input,init){
  if(typeof input==="string"||input instanceof URL)return originalFetch(add(String(input)),init);
  if(input instanceof Request){var next=add(input.url);if(next!==input.url)input=new Request(next,input);}
  return originalFetch(input,init);
};
var originalOpen=XMLHttpRequest.prototype.open;
XMLHttpRequest.prototype.open=function(method,url){arguments[1]=add(String(url));return originalOpen.apply(this,arguments);};
if(navigator.sendBeacon){var originalBeacon=navigator.sendBeacon.bind(navigator);navigator.sendBeacon=function(url,data){return originalBeacon(add(String(url)),data);};}
var originalWindowOpen=window.open;window.open=function(url){if(typeof url==="string")arguments[0]=add(url);return originalWindowOpen.apply(window,arguments);};
document.addEventListener("click",function(event){var node=event.target&&event.target.closest?event.target.closest("a[href]"):null;if(!node)return;var raw=node.getAttribute("href");var next=add(raw);if(next!==raw)node.setAttribute("href",next);},true);
document.addEventListener("submit",function(event){var form=event.target;if(!form||!form.getAttribute)return;var raw=form.getAttribute("action");var next=add(raw);if(next!==raw)form.setAttribute("action",next);},true);
})(${encoded});`;
}

declare global {
  interface Window {
    __atBasePathInstalled?: boolean;
    __atWithBasePath?: (value: string) => string;
  }
}
