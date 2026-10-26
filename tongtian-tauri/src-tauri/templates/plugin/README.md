# {{name}} — 通天办公套件

这个目录是一个可安装的专家套件（插件）包。打包后从「专家套件 → 添加 → 上传套件」安装。

## 目录结构

```
plugin.json          必填，套件清单（根目录或单层目录内）
commands/*.md        指令正文；用户在对话框输入 /指令名 时取用
skills/*/SKILL.md    技能正文；模型需要时通过 use_skill 取用
```

## plugin.json 字段

- `name`：必填，套件展示名（安装目录标识由名称生成，同名套件不覆盖）
- `version`、`description`、`author`、`category`：可选，展示用
- `commands`：数组或对象，支持三种写法：`["名称"]`、`[{name,description,file|content}]`、`{名称: 描述}`
- `skills`：可选。即使不声明，包内 `**/SKILL.md` 也会被扫描并进入执行面

`mcpServers`、`hooks`、`agents` 等字段本应用不会执行，校验会明确列出被忽略的字段。

## 本地校验与打包

```bash
node scripts/plugin-sdk.mjs init ./my-plugin --name "我的套件"
node scripts/plugin-sdk.mjs validate ./my-plugin
node scripts/plugin-sdk.mjs pack ./my-plugin -o my-plugin.zip
```

桌面版也可以在「专家套件 → 添加 → 插件 SDK」里选择 .zip 做安装前校验。
