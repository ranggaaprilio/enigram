import type {SidebarsConfig} from '@docusaurus/plugin-content-docs';

// Doc ids are paths under the repository's docs/ folder, without `.md`.
const sidebars: SidebarsConfig = {
  docs: [
    'intro',
    {
      type: 'category',
      label: 'Getting started',
      collapsed: false,
      items: [
        'getting-started/quickstart',
        'getting-started/local-setup',
        'getting-started/model-providers',
      ],
    },
    {
      type: 'category',
      label: 'Guides',
      collapsed: false,
      items: [
        'guides/web-ui',
        'guides/repositories',
        'guides/notes',
        'guides/correcting-knowledge',
        'guides/mcp',
        'guides/cli',
      ],
    },
    {
      type: 'category',
      label: 'Self-hosting',
      items: ['self-hosting/deployment', 'self-hosting/access-control'],
    },
    {
      type: 'category',
      label: 'Reference',
      items: ['reference/http-api', 'reference/roles', 'reference/decision-layer'],
    },
    {
      type: 'category',
      label: 'Evaluation',
      items: ['evaluation/how-it-works', 'evaluation/golden-set'],
    },
    {
      type: 'category',
      label: 'Project',
      items: [
        'project/design',
        'project/third-party-licenses',
        {
          type: 'link',
          label: 'Contributing',
          href: 'https://github.com/ranggaaprilio/enigram/blob/main/CONTRIBUTING.md',
        },
      ],
    },
  ],
};

export default sidebars;
