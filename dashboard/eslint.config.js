// ESLint flat config. Lint scope is src/ only — vite.config.ts and the build
// output are intentionally outside it.
//
// typescript-eslint runs non-type-checked (the plain `recommended` presets,
// not `recommendedTypeChecked`): the build currently carries known tsc errors,
// and type-aware linting would just re-report them as lint noise.
import { defineConfig, globalIgnores } from 'eslint/config'
import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import tseslint from 'typescript-eslint'

export default defineConfig([
  globalIgnores(['dist']),

  // App code: base JS rules + non-type-checked typescript-eslint.
  {
    files: ['src/**/*.{ts,tsx}'],
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    languageOptions: {
      ecmaVersion: 2022,
      globals: globals.browser,
    },
    rules: {
      // Standard TS conventions: a `_`-prefixed name is deliberately unused,
      // and a binding omitted via destructuring-with-rest (`const { a, ...rest }`)
      // exists only to strip the field. tsc's noUnusedLocals honours both.
      '@typescript-eslint/no-unused-vars': [
        'error',
        { argsIgnorePattern: '^_', varsIgnorePattern: '^_', ignoreRestSiblings: true },
      ],
    },
  },

  // Hooks: the two classic rules. The plugin's v7 `recommended` preset also
  // turns on the React-compiler-derived rules (purity, immutability,
  // set-state-in-effect, ...) at error level; satisfying those across this
  // codebase is a separate project, so they are left off for now.
  {
    files: ['src/**/*.{ts,tsx}'],
    plugins: {
      'react-hooks': reactHooks,
    },
    rules: {
      'react-hooks/rules-of-hooks': 'error',
      'react-hooks/exhaustive-deps': 'warn',
    },
  },

  // Fast refresh: only meaningful in files that actually render components.
  // Severity is warn, not error: quite a few files here export a component
  // next to a helper or constant, and splitting them is a refactor to do on
  // its own — the warning keeps the debt visible without blocking lint.
  // allowConstantExport matches the plugin's Vite preset: a plain const next
  // to a component does not break HMR.
  {
    files: ['src/**/*.tsx'],
    plugins: {
      'react-refresh': reactRefresh,
    },
    rules: {
      'react-refresh/only-export-components': ['warn', { allowConstantExport: true }],
    },
  },

  // Config files (including this one) run under Node, not the browser.
  // vite.config.ts is TypeScript, so it needs the TS parser.
  {
    files: ['*.config.{js,ts}', 'eslint.config.js'],
    languageOptions: {
      parser: tseslint.parser,
      globals: globals.node,
    },
  },
])
