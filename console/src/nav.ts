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
  /** Roles that see the page; empty means every signed-in user. */
  roles?: string[];
}

export const PAGES: NavPage[] = [
  { path: '/', label: 'Home', icon: IconGauge, phase: '0', summary: 'Platform health and recent runs.' },
  {
    path: '/connections',
    label: 'Connections',
    icon: IconPlugConnected,
    phase: '1',
    summary: 'Sources and targets: files (local, SMB, FTP, SFTP, object stores), databases, APIs and event streams.',
  },
  {
    path: '/ingestion',
    label: 'Ingestion Jobs',
    icon: IconDatabaseImport,
    phase: '1',
    summary: 'The simple ETL wizard: pick a connection, a table/file template/endpoint, a load mode and a schedule.',
  },
  {
    path: '/streams',
    label: 'Streams & Replication',
    icon: IconBolt,
    phase: '1b',
    summary: 'CDC replication and event streams with live lag, latency, throughput and dead-letter queues.',
  },
  {
    path: '/pipelines',
    label: 'Pipelines',
    icon: IconRoute,
    phase: '2',
    summary: 'SQL/Python models promoting data bronze → silver → gold, with incremental and streaming runs.',
  },
  {
    path: '/data-vault',
    label: 'Data Vault',
    icon: IconBuildingWarehouse,
    phase: '2',
    summary: 'Design hubs, links and satellites (Data Vault 2.0), plus PIT and bridge tables.',
  },
  {
    path: '/catalog',
    label: 'Catalog',
    icon: IconBook2,
    phase: '1',
    summary: 'Search datasets, columns, glossary terms, tags and classifications.',
  },
  {
    path: '/lineage',
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
    path: '/portal',
    label: 'App Portal',
    icon: IconApps,
    phase: '1',
    summary: 'Links to the reports, dashboards and business apps built on the platform.',
  },
  {
    path: '/admin',
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
