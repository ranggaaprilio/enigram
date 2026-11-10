import {themes as prismThemes} from 'prism-react-renderer';
import type {Config} from '@docusaurus/types';
import type * as Preset from '@docusaurus/preset-classic';

const repo = 'https://github.com/ranggaaprilio/enigram';

const config: Config = {
  title: 'Enigram',
  tagline:
    "Ask your codebase and its docs. Get answers pinned to the commit, with your team's notes on top.",
  favicon: 'img/favicon.svg',

  future: {
    v4: true,
  },

  // GitHub Pages: https://ranggaaprilio.github.io/enigram/
  url: 'https://ranggaaprilio.github.io',
  baseUrl: '/enigram/',
  organizationName: 'ranggaaprilio',
  projectName: 'enigram',
  trailingSlash: false,

  onBrokenLinks: 'throw',
  onBrokenAnchors: 'throw',

  markdown: {
    // .md files are plain CommonMark (no JSX), so `<id>` and `{name}` in prose are safe.
    // Admonitions still work; a title goes in brackets: :::tip[Cost].
    format: 'detect',
    mermaid: true,
    hooks: {
      onBrokenMarkdownLinks: 'throw',
    },
  },
  themes: ['@docusaurus/theme-mermaid'],

  i18n: {
    defaultLocale: 'en',
    locales: ['en'],
  },

  stylesheets: [
    {
      href: 'https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap',
      type: 'text/css',
    },
  ],

  presets: [
    [
      'classic',
      {
        docs: {
          // The Markdown lives in the repository's docs/ folder, so GitHub renders it too.
          path: '../docs',
          sidebarPath: './sidebars.ts',
          editUrl: ({docPath}) => `${repo}/edit/main/docs/${docPath}`,
        },
        blog: false,
        theme: {
          customCss: './src/css/custom.css',
        },
      } satisfies Preset.Options,
    ],
  ],

  themeConfig: {
    colorMode: {
      respectPrefersColorScheme: true,
    },
    announcementBar: {
      id: 'alpha',
      content:
        'Enigram is <strong>alpha</strong> software: expect breaking changes between versions.',
      isCloseable: true,
    },
    docs: {
      sidebar: {
        hideable: true,
      },
    },
    navbar: {
      title: 'Enigram',
      logo: {
        alt: 'Enigram logo',
        src: 'img/logo.svg',
        srcDark: 'img/logo-dark.svg',
      },
      items: [
        {type: 'docSidebar', sidebarId: 'docs', position: 'left', label: 'Docs'},
        {to: '/docs/getting-started/quickstart', label: 'Quickstart', position: 'left'},
        {to: '/docs/reference/http-api', label: 'API', position: 'left'},
        {href: repo, label: 'GitHub', position: 'right'},
      ],
    },
    footer: {
      style: 'dark',
      links: [
        {
          title: 'Docs',
          items: [
            {label: 'Introduction', to: '/docs/'},
            {label: 'Quickstart', to: '/docs/getting-started/quickstart'},
            {label: 'Claude Code (MCP)', to: '/docs/guides/mcp'},
          ],
        },
        {
          title: 'Reference',
          items: [
            {label: 'HTTP API', to: '/docs/reference/http-api'},
            {label: 'Roles and permissions', to: '/docs/reference/roles'},
            {label: 'Command line', to: '/docs/guides/cli'},
          ],
        },
        {
          title: 'Project',
          items: [
            {label: 'GitHub', href: repo},
            {label: 'Contributing', href: `${repo}/blob/main/CONTRIBUTING.md`},
            {label: 'License (AGPL-3.0-only)', href: `${repo}/blob/main/LICENSE`},
          ],
        },
      ],
      copyright: `Copyright © ${new Date().getFullYear()} Rangga Aprilio Utama and contributors. "Enigram" and its logo are not covered by the license.`,
    },
    prism: {
      theme: prismThemes.github,
      darkTheme: prismThemes.vsDark,
      additionalLanguages: ['bash', 'yaml', 'json'],
    },
    mermaid: {
      theme: {light: 'neutral', dark: 'dark'},
    },
  } satisfies Preset.ThemeConfig,
};

export default config;
