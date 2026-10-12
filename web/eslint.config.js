import ts from 'typescript-eslint'
import vue from 'eslint-plugin-vue'
export default [...ts.configs.recommended,...vue.configs['flat/recommended'],{files:['**/*.vue'],languageOptions:{parserOptions:{parser:ts.parser}},rules:{'vue/multi-word-component-names':'off','vue/max-attributes-per-line':'off','vue/html-self-closing':'off','vue/singleline-html-element-content-newline':'off'}}, {rules:{'@typescript-eslint/no-explicit-any':'error'}}]
