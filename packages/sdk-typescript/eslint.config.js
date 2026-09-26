import js from "@eslint/js";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["dist/**", "node_modules/**"] },
  js.configs.recommended,
  ...tseslint.configs.strict,
  {
    languageOptions: { globals: { process: "readonly", console: "readonly" } },
    rules: {
      // Telemetry must never throw into the host: some catch blocks are empty on purpose.
      "no-empty": ["error", { allowEmptyCatch: true }],
      // noUncheckedIndexedAccess types every index as possibly undefined; an
      // assertion after a bounds check is the plain way to say it is not.
      "@typescript-eslint/no-non-null-assertion": "off",
      "@typescript-eslint/no-unused-vars": ["error", { argsIgnorePattern: "^_" }],
    },
  },
);
