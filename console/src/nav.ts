import {
  IconApps,
  IconBolt,
  IconBook2,
  IconBuildingWarehouse,
  IconChecklist,
  IconDatabaseImport,
  IconGauge,
  IconHierarchy3,
  IconPlugConnected,
  IconRoute,
  IconSend,
  IconShieldLock,
  IconTopologyStar3,
  IconUsersGroup,
  type Icon,
} from '@tabler/icons-react';

export interface NavPage {
  path: string;
  label: string;
  icon: Icon;
  /** Build phase that delivers the page (docs/PLATFORM_PLAN.md). */
  phase: string;
  summary: string;
  /** True once the page is built (otherwise it shows a "coming in Phase N" placeholder). */
  ready?: boolean;
  /** Roles that see the page; empty means every signed-in user. */
  roles?: string[];
}

export const PAGES: NavPage[] = [
  { path: '/', ready: true, label: 'Home', icon: IconGauge, phase: '0', summary: 'Platform health and recent runs.' },
  {
    path: '/connections', ready: true,
    label: 'Connections',
    icon: IconPlugConnected,
    phase: '1',
    summary: 'Sources and targets: files (local, SMB, FTP, SFTP, object stores), databases, APIs and event streams.',
  },
  {
    path: '/ingestion', ready: true,
    label: 'Ingestion Jobs',
    icon: IconDatabaseImport,
    phase: '1',
    summary: 'The simple ETL wizard: pick a connection, a table/file template/endpoint, a load mode and a schedule.',
  },
  {
    path: '/streams',
    ready: true,
    label: 'Streams & Replication',
    icon: IconBolt,
    phase: '1b',
    summary: 'CDC replication and event streams with live lag, latency, throughput and dead-letter queues.',
  },
  {
    path: '/pipelines',
    ready: true,
    label: 'Pipelines',
    icon: IconRoute,
    phase: '2',
    summary: 'SQL models, SCD2 dimensions and facts promoting data bronze → silver → gold, incrementally and on every change.',
  },
  {
    path: '/data-vault',
    ready: true,
    label: 'Data Vault',
    icon: IconBuildingWarehouse,
    phase: '2',
    summary: 'Design hubs, links and satellites (Data Vault 2.0), plus PIT and bridge tables.',
  },
  {
    path: '/catalog', ready: true,
    label: 'Catalog',
    icon: IconBook2,
    phase: '1',
    summary: 'Search datasets, columns, glossary terms, tags and classifications.',
  },
  {
    path: '/lineage', ready: true,
    label: 'Lineage',
    icon: IconHierarchy3,
    phase: '1',
    summary: 'End-to-end lineage from source to report, with column-level tracing and impact analysis.',
  },
  {
    path: '/ontology',
    label: 'Ontology Studio',
    icon: IconTopologyStar3,
    phase: '3b',
    summary: 'Ontologies, semantic mappings, the knowledge graph, SPARQL and entity resolution.',
  },
  {
    path: '/quality',
    label: 'Data Quality',
    icon: IconChecklist,
    phase: '3',
    summary: 'Profiles, steward rules, scorecards and degradation alerts.',
  },
  {
    path: '/governance',
    label: 'Governance',
    icon: IconShieldLock,
    phase: '3',
    summary: 'Tags, classifications, masking and row-filter policies.',
  },
  {
    path: '/delivery',
    label: 'Delivery',
    icon: IconSend,
    phase: '4',
    summary: 'Extracts, generated REST/GraphQL APIs and the serving database.',
  },
  {
    path: '/portal', ready: true,
    label: 'App Portal',
    icon: IconApps,
    phase: '1',
    summary: 'Links to the reports, dashboards and business apps built on the platform.',
  },
  {
    path: '/admin', ready: true,
    label: 'Admin',
    icon: IconUsersGroup,
    phase: '0',
    summary: 'Users, groups, roles, service accounts, secrets and the audit log.',
    roles: ['admin'],
  },
];

export const ADMIN_TABS = [
  { path: '/admin/users', label: 'Users' },
  { path: '/admin/groups', label: 'Groups' },
  { path: '/admin/service-accounts', label: 'Service accounts & secrets' },
  { path: '/admin/audit', label: 'Audit log' },
];
