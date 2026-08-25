import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";

const eslintConfig = defineConfig([
  ...nextVitals,
  ...nextTs,
  {
    files: ["**/*.{ts,tsx}"],
    rules: {
      "no-restricted-syntax": [
        "error",
        {
          selector: "JSXOpeningElement[name.name='select']",
          message:
            "OS依存のnative selectではなく、@/components/ui/app-select または @/components/ui/select を使用してください。",
        },
        {
          selector:
            "JSXOpeningElement[name.name='input'] > JSXAttribute[name.name='type'][value.value='checkbox']",
          message:
            "native checkboxではなく、@/components/ui/checkbox を使用してください。",
        },
      ],
    },
  },
  // Override default ignores of eslint-config-next.
  globalIgnores([
    // Default ignores of eslint-config-next:
    ".next/**",
    ".next-*/**",
    "out/**",
    "build/**",
    "temp/**",
    "next-env.d.ts",
    // OpenAPI から自動生成する型定義（手編集禁止・lint 対象外）
    "src/lib/api-types.gen.ts",
  ]),
]);

export default eslintConfig;
