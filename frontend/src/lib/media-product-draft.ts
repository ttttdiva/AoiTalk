import type { DlsiteContentPayload, FanzaContentPayload } from "./media-operations-content-api";

export type ProductPlatform = "dlsite" | "fanza";
export type ProductPayload = DlsiteContentPayload | FanzaContentPayload;
export type ProductDraft = {
  title: string; description: string; category: string; age_rating: string;
  price: string; currency: string; tax_included: boolean; distribution: string;
  preview_assets: string; thumbnail_assets: string; deliverable_package_ref: string;
  rights_checklist: ProductPayload["rights_checklist"];
  storefront: FanzaContentPayload["storefront"]; seller_ref: string;
  ai_usage: FanzaContentPayload["ai_usage"]; submission_state: FanzaContentPayload["submission_state"];
};
const refs = (value: string) => [...new Set(value.split(/[\n,]/u).map(v => v.trim()).filter(Boolean))];
export const isProductPlatform = (value: string): value is ProductPlatform => value === "dlsite" || value === "fanza";
export function emptyProductDraft(): ProductDraft {
  return { title: "", description: "", category: "", age_rating: "all_ages", price: "", currency: "JPY",
    tax_included: true, distribution: "download", preview_assets: "", thumbnail_assets: "", deliverable_package_ref: "",
    rights_checklist: [], storefront: "doujin", seller_ref: "", ai_usage: "unknown", submission_state: "draft" };
}
export function productDraftFromPayload(payload: ProductPayload): ProductDraft {
  return { ...emptyProductDraft(), title: payload.title, description: payload.description, category: payload.category,
    age_rating: payload.age_rating, price: String(payload.price), currency: payload.sales.currency,
    tax_included: payload.sales.tax_included, distribution: payload.sales.distribution,
    preview_assets: payload.preview_assets.join("\n"), thumbnail_assets: (payload.thumbnail_assets ?? []).join("\n"),
    deliverable_package_ref: payload.deliverable_package_ref, rights_checklist: payload.rights_checklist.map(item => ({ ...item })),
    ...(payload.type === "fanza_release" ? { storefront: payload.storefront, seller_ref: payload.seller_ref,
      ai_usage: payload.ai_usage, submission_state: payload.submission_state } : {}) };
}
export function productDraftError(platform: ProductPlatform, form: ProductDraft): string | null {
  const name = platform === "fanza" ? "FANZA" : "DLsite";
  if (!form.title.trim() || !form.description.trim() || !form.category.trim() || !form.deliverable_package_ref.trim()) return `${name}商品タイトル・説明・カテゴリ・配布パッケージ参照を入力してください`;
  if (!refs(form.preview_assets).length) return `${name} Preview Assetを1件以上入力してください`;
  if (!form.price.trim() || !Number.isFinite(Number(form.price)) || Number(form.price) < 0 || Number(form.price) > 10000000) return `${name}価格を0以上で入力してください`;
  if (!/^[a-z]{3,8}$/iu.test(form.currency.trim()) || !form.distribution.trim()) return `${name}通貨・配布方式を入力してください`;
  if (!form.rights_checklist.length || form.rights_checklist.some(item => !item.code.trim())) return `${name} Rights checklistを1件以上入力してください`;
  if (platform === "fanza" && !form.seller_ref.trim()) return "FANZA販売者参照を入力してください";
  return null;
}
export function productPayloadFromDraft(platform: ProductPlatform, form: ProductDraft): ProductPayload {
  const error = productDraftError(platform, form);
  if (error) throw new Error(error);
  const common = { title: form.title.trim(), description: form.description.trim(), category: form.category.trim(),
    age_rating: form.age_rating, price: Number(form.price), sales: { currency: form.currency.trim().toUpperCase(), tax_included: form.tax_included, distribution: form.distribution.trim() },
    preview_assets: refs(form.preview_assets), thumbnail_assets: refs(form.thumbnail_assets), deliverable_package_ref: form.deliverable_package_ref.trim(),
    rights_checklist: form.rights_checklist.map(item => ({ ...item, code: item.code.trim(), note: item.note ?? null })) };
  return platform === "fanza" ? { ...common, type: "fanza_release", storefront: form.storefront, seller_ref: form.seller_ref.trim(), ai_usage: form.ai_usage, ai_generated: form.ai_usage === "generated", submission_state: form.submission_state }
    : { ...common, type: "dlsite_release" };
}
