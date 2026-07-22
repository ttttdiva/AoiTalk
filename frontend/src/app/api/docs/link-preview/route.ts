import { lookup } from "node:dns/promises";
import { isIP } from "node:net";
import { NextRequest, NextResponse } from "next/server";
import { getSession } from "@/lib/auth";

function textBetween(html: string, pattern: RegExp) {
  const match = html.match(pattern);
  return match?.[1]?.replace(/\s+/g, " ").trim() ?? "";
}

function isPrivateIp(address: string) {
  if (address === "::1") return true;
  if (address.toLowerCase().startsWith("fc") || address.toLowerCase().startsWith("fd")) return true;
  const parts = address.split(".").map((part) => Number(part));
  if (parts.length !== 4 || parts.some((part) => !Number.isInteger(part))) return false;
  return (
    parts[0] === 10 ||
    parts[0] === 127 ||
    (parts[0] === 169 && parts[1] === 254) ||
    (parts[0] === 172 && parts[1] >= 16 && parts[1] <= 31) ||
    (parts[0] === 192 && parts[1] === 168)
  );
}

async function assertPublicTarget(url: URL) {
  if (url.hostname === "localhost" || isPrivateIp(url.hostname)) throw new Error("private host");
  if (isIP(url.hostname)) return;
  const addresses = await lookup(url.hostname, { all: true });
  if (addresses.some((address) => isPrivateIp(address.address))) throw new Error("private host");
}

export async function POST(request: NextRequest) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "Authentication required" }, { status: 401 });
  }
  const body = await request.json().catch(() => ({}));
  const rawUrl = typeof body.url === "string" ? body.url : "";
  let parsed: URL;
  try {
    parsed = new URL(rawUrl);
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") throw new Error("invalid protocol");
  } catch {
    return NextResponse.json({ detail: "Invalid URL" }, { status: 400 });
  }

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 3500);
  try {
    await assertPublicTarget(parsed);
    const response = await fetch(parsed.toString(), {
      signal: controller.signal,
      headers: {
        "User-Agent": "AoiTalk Docs Link Preview",
        Accept: "text/html,application/xhtml+xml",
      },
      redirect: "follow",
    });
    const html = await response.text();
    const title =
      textBetween(html, /<meta[^>]+property=["']og:title["'][^>]+content=["']([^"']+)["'][^>]*>/i) ||
      textBetween(html, /<meta[^>]+content=["']([^"']+)["'][^>]+property=["']og:title["'][^>]*>/i) ||
      textBetween(html, /<title[^>]*>([^<]+)<\/title>/i) ||
      parsed.hostname;
    const description =
      textBetween(html, /<meta[^>]+property=["']og:description["'][^>]+content=["']([^"']+)["'][^>]*>/i) ||
      textBetween(html, /<meta[^>]+name=["']description["'][^>]+content=["']([^"']+)["'][^>]*>/i);
    const domain = parsed.hostname.replace(/^www\./, "");
    return NextResponse.json({
      url: parsed.toString(),
      title,
      description,
      domain,
      favicon: new URL("/favicon.ico", parsed.origin).toString(),
    });
  } catch {
    const domain = parsed.hostname.replace(/^www\./, "");
    return NextResponse.json({
      url: parsed.toString(),
      title: domain,
      description: "",
      domain,
      favicon: new URL("/favicon.ico", parsed.origin).toString(),
    });
  } finally {
    clearTimeout(timer);
  }
}
