---
name: frontend-project-scaffolder
description: Creates production-ready frontend projects with modern tooling. Use when initializing a new React/Vue/Next.js project, setting up build configuration, or scaffolding a complete frontend repository with best practices.
---

# Frontend Project Scaffolder

## Overview

Create production-grade frontend projects with proper tooling, configuration, and structure. This skill handles the complete project initialization workflow from scratch to a working, git-initialized repository.

## Constraints

**All projects created by this skill MUST comply with:**

- [`frontend-agent-constraints.md`](../../../../references/frontend-agent-constraints.md) - 前端开发约束规范（最高优先级）

**Key constraints for project setup:**

- State management: **Zustand** (no Redux/MobX)
- Code quality: Single file ≤ 300 lines, no `any` types
- UI library: **lucide** icons (no emoji)
- Animation: **framer-motion** for interactions
- Security: No hardcoded secrets, no `dangerouslySetInnerHTML`

## When to Use

- Starting a new frontend project
- Setting up build tooling and configuration
- Creating a repository for a new web application
- Migrating from old stack to modern tooling
- When user asks for "项目"、"仓库"、"完整的前端工程"

## Technology Stack Selection

### Default Stack (Recommended)

```
Framework:      Preact 10+ with TypeScript
Build Tool:     Vite 6+
Styling:        Tailwind CSS 4+ (Vite plugin mode)
State:          Zustand 5+ (client)
Routing:        preact-router 4+
HTTP:           Ky 1+
Linting:        ESLint 9+ (flat config)
Package Manager: npm or pnpm
```

### Alternative Stacks

- **React 18+**: React Router v6, TanStack Query, shadcn/ui
- **Vue 3**: Pinia, Element Plus/Naive UI
- **Astro 4+**: Islands architecture, static/SSR

## Project Creation Workflow

### Step 1: Gather Requirements

Clarify before creating:

- Project name, framework choice (default: Preact)
- Features needed (routing/state/auth/i18n)
- Deployment target, team size

If unspecified, use defaults and state assumptions.

### Step 2: Initialize Project

**Preact + Vite (Default)**:

```bash
npm create vite@latest <project-name> -- --template preact-ts
cd <project-name>
npm install
```

**React**: `npm create vite@latest <project-name> -- --template react-ts`  
**Vue 3**: `npm create vue@latest <project-name>`

### Step 3: Install Core Dependencies

```bash
# Tailwind CSS 4 (Vite plugin)
npm install -D tailwindcss @tailwindcss/vite

# Routing & State
npm install preact-router zustand ky

# Dev Tools
npm install -D typescript-eslint
```

### Step 4: Configure Tailwind

Tailwind CSS 4 uses Vite plugin mode - **no tailwind.config.js needed**.

**Key rule**: Define **complete color scale** (50-900) in `@theme` to generate all utility classes:

```css
/* src/index.css */
@import 'tailwindcss';

@theme {
  /* Must define ALL steps */
  --color-primary-50: #f0f9ff;
  --color-primary-100: #e0f2fe;
  --color-primary-200: #bae6fd;
  --color-primary-300: #7dd3fc;
  --color-primary-400: #38bdf8;
  --color-primary-500: #0ea5e9;
  --color-primary-600: #0284c7;
  --color-primary-700: #0369a1;
  --color-primary-800: #075985;
  --color-primary-900: #0c4a6e;

  --font-sans: Inter, system-ui, sans-serif;
}

@layer base {
  /* CSS Reset */
  *,
  *::before,
  *::after {
    box-sizing: border-box;
    margin: 0;
    padding: 0;
    border: 0;
  }

  body {
    line-height: 1.5;
    -webkit-font-smoothing: antialiased;
    -moz-osx-font-smoothing: grayscale;
    text-rendering: optimizeLegibility;
  }

  ul,
  ol {
    list-style: none;
  }
  h1,
  h2,
  h3,
  h4,
  h5,
  h6 {
    font-size: inherit;
    font-weight: inherit;
  }
  a {
    color: inherit;
    text-decoration: inherit;
  }

  img,
  svg,
  video,
  canvas {
    display: block;
    max-width: 100%;
  }

  button,
  input,
  select,
  textarea {
    font: inherit;
    color: inherit;
  }

  table {
    border-collapse: collapse;
    border-spacing: 0;
  }
}
```

⚠️ **Critical**: Missing any step (e.g., only 50/500/900) will cause "Cannot apply unknown utility class" errors when using `hover:bg-primary-600`.

### Step 5: Setup ESLint

Use ESLint 9+ flat config. Add to `package.json`:

```json
{
  "scripts": {
    "lint": "eslint . --ext .ts,.tsx",
    "lint:fix": "eslint . --ext .ts,.tsx --fix"
  }
}
```

### Step 6: Create Project Structure

```
project-name/
├── src/
│   ├── main.tsx              # Entry point
│   ├── App.tsx               # Root component
│   ├── index.css             # Tailwind @theme
│   ├── components/           # Reusable UI
│   │   ├── ui/              # Base components (Button, Card, Input)
│   │   └── Layout/          # Header, Sidebar, Footer
│   ├── pages/               # Route pages
│   ├── hooks/               # Custom hooks
│   ├── stores/              # Zustand stores
│   ├── services/            # API layer
│   └── types/               # TypeScript types
├── package.json
├── vite.config.ts
└── eslint.config.js
```

### Step 7: Create Base Files

**main.tsx** (Preact):

```tsx
import { render } from 'preact';
import { Router } from 'preact-router';
import App from './App';
import './index.css';

render(<App />, document.getElementById('root')!);
```

**App.tsx**:

```tsx
import { Router } from 'preact-router';
import { Layout } from './components/Layout';
import { HomePage } from './pages/HomePage';

function App() {
  return (
    <Router>
      <Layout path='/'>
        <HomePage path='/' />
      </Layout>
    </Router>
  );
}
```

### Step 8: Setup Git

```bash
git init
git add .
git commit -m "chore: init with Vite + Preact + TypeScript + Tailwind CSS 4"
```

### Step 9: Create README

Include: tech stack, installation steps, development commands, deployment guide.

### Step 10: Verify Project

```bash
npm install && npm run lint && npm run build && npx tsc --noEmit
```

All should pass without errors.

## Common Project Templates

- **Enterprise Dashboard**: Sidebar, data tables, charts, forms
- **E-commerce**: Product listing, cart, checkout, auth
- **SaaS Landing**: Hero, features, pricing, testimonials
- **Mobile PWA**: Service worker, offline support, push notifications

## Post-Creation Tasks

- [ ] Update README
- [ ] Configure CI/CD
- [ ] Setup error tracking (Sentry)
- [ ] Configure i18n if needed
- [ ] Setup testing framework

## Quality Checklist

- [ ] `npm run build` succeeds
- [ ] `tsc --noEmit` passes
- [ ] `npm run lint` clean
- [ ] Git initialized
- [ ] Tailwind `@theme` configured with complete color scale
- [ ] Routing & state management setup

## Common Issues

### Tailwind CSS 4: "Cannot apply unknown utility class"

**Cause**: `@theme` missing color steps.

**Fix**: Define complete scale (50-900):

```css
@theme {
  --color-primary-50: #f0f9ff;
  /* ... all steps 50-900 ... */
  --color-primary-900: #0c4a6e;
}
```

**Alternative**: Use Tailwind built-in colors (`bg-sky-500`).

### ESLint errors

- Check plugin compatibility
- Verify flat config format (ESLint 9+)

### TypeScript errors

- Check `tsconfig.json` paths
- Ensure `@types/*` installed

## Integration with Design Skills

After scaffolding:

1. **Use `ui-ux-pro-max`** to generate design system
2. **Update `@theme`** in `index.css` with design tokens
3. **Use `frontend-ui-designer`** to implement components
4. **Follow project conventions** established here
