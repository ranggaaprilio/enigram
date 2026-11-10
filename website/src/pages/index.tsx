import type {ReactNode} from 'react';
import Link from '@docusaurus/Link';
import useDocusaurusContext from '@docusaurus/useDocusaurusContext';
import useBaseUrl from '@docusaurus/useBaseUrl';
import ThemedImage from '@theme/ThemedImage';
import CodeBlock from '@theme/CodeBlock';
import Layout from '@theme/Layout';
import Heading from '@theme/Heading';
// The screenshots live with the docs (the README uses them too).
import shotLight from '../../../docs/assets/screenshot-chat-light.png';
import shotDark from '../../../docs/assets/screenshot-chat-dark.png';

import styles from './index.module.css';

const FEATURES: {title: string; body: string; to: string}[] = [
  {
    title: 'Citations pinned to a commit',
    body: 'Every code citation is file:line@sha, so it stays checkable after the code moves on.',
    to: '/docs/guides/repositories',
  },
  {
    title: "Your team's notes win",
    body: 'Verified notes on symbols outrank code summaries and documents in every answer.',
    to: '/docs/guides/notes',
  },
  {
    title: 'Notes go stale on their own',
    body: 'A note is flagged when its symbol or a callee changes, and orphaned with relink candidates when the symbol is gone.',
    to: '/docs/guides/notes#staying-current',
  },
  {
    title: 'An origin on every sentence',
    body: 'Each sentence says where it came from: [code], [summary], [human], [document] or [unsupported].',
    to: '/docs/guides/notes#origins-and-warnings',
  },
  {
    title: 'Agents for each reader',
    body: 'Technical, Business rules, QA and Overview agents in one chat, or a panel of three at once.',
    to: '/docs/guides/web-ui#chat',
  },
  {
    title: 'Built for coding agents',
    body: 'An MCP server for Claude Code: ask, remember, see what changed, and propose notes for review.',
    to: '/docs/guides/mcp',
  },
];

const QUICKSTART = `git clone https://github.com/ranggaaprilio/enigram && cd enigram
cp .env.example .env    # fill in DEEPSEEK_API_KEY and VOYAGE_API_KEY
docker compose up -d --build
# open http://localhost:3000`;

function Hero(): ReactNode {
  const {siteConfig} = useDocusaurusContext();
  return (
    <header className={styles.hero}>
      <div className="container">
        <ThemedImage
          className={styles.logo}
          alt=""
          sources={{light: useBaseUrl('/img/logo.svg'), dark: useBaseUrl('/img/logo-dark.svg')}}
        />
        <Heading as="h1" className={styles.title}>
          {siteConfig.title}
        </Heading>
        <p className={styles.tagline}>{siteConfig.tagline}</p>
        <div className={styles.buttons}>
          <Link className="button button--primary button--lg" to="/docs/getting-started/quickstart">
            Get started
          </Link>
          <Link className="button button--secondary button--lg" to="/docs/">
            What is Enigram?
          </Link>
        </div>
        <p className={styles.meta}>
          Self-hosted · Postgres · TypeScript (NestJS, Next.js, Express) · AGPL-3.0-only
        </p>
        <ThemedImage
          className={styles.screenshot}
          alt="The Chat page: an answer with a source pinned to file:line@sha, then a panel of the Business rules and QA agents"
          sources={{light: shotLight, dark: shotDark}}
        />
      </div>
    </header>
  );
}

function Features(): ReactNode {
  return (
    <section className={styles.section}>
      <div className="container">
        <div className={styles.grid}>
          {FEATURES.map((f) => (
            <Link key={f.title} to={f.to} className={styles.card}>
              <Heading as="h3">{f.title}</Heading>
              <p>{f.body}</p>
            </Link>
          ))}
        </div>
      </div>
    </section>
  );
}

function Quickstart(): ReactNode {
  return (
    <section className={styles.section}>
      <div className={`container ${styles.narrow}`}>
        <Heading as="h2">Run it in three commands</Heading>
        <p>
          You need Docker, a DeepSeek key and a Voyage AI key, or any{' '}
          <Link to="/docs/getting-started/model-providers">OpenAI-compatible endpoint</Link>.
        </p>
        <CodeBlock language="bash">{QUICKSTART}</CodeBlock>
        <Link to="/docs/getting-started/quickstart">Index your first repository →</Link>
      </div>
    </section>
  );
}

export default function Home(): ReactNode {
  const {siteConfig} = useDocusaurusContext();
  return (
    <Layout title="Ask your codebase" description={siteConfig.tagline}>
      <Hero />
      <main>
        <Features />
        <Quickstart />
      </main>
    </Layout>
  );
}
