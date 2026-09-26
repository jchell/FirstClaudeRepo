import { Stack, Tabs, Title } from '@mantine/core';
import { Navigate, Route, Routes, useLocation, useNavigate } from 'react-router-dom';

import { ADMIN_TABS } from '../../nav';
import { AuditPage } from './AuditPage';
import { GroupsPage } from './GroupsPage';
import { NotificationsPage } from './NotificationsPage';
import { ServiceAccountsPage } from './ServiceAccountsPage';
import { UsersPage } from './UsersPage';
import { VaultAuditPage } from './VaultAuditPage';

export function AdminPage() {
  const location = useLocation();
  const navigate = useNavigate();
  const current = ADMIN_TABS.find((t) => location.pathname.startsWith(t.path))?.path ?? ADMIN_TABS[0].path;

  return (
    <Stack>
      <Title order={2}>Admin</Title>
      <Tabs value={current} onChange={(v) => v && navigate(v)}>
        <Tabs.List>
          {ADMIN_TABS.map((t) => (
            <Tabs.Tab key={t.path} value={t.path}>
              {t.label}
            </Tabs.Tab>
          ))}
        </Tabs.List>
      </Tabs>
      <Routes>
        <Route index element={<Navigate to="users" replace />} />
        <Route path="users" element={<UsersPage />} />
        <Route path="groups" element={<GroupsPage />} />
        <Route path="service-accounts" element={<ServiceAccountsPage />} />
        <Route path="audit" element={<AuditPage />} />
        <Route path="vault-audit" element={<VaultAuditPage />} />
        <Route path="notifications" element={<NotificationsPage />} />
      </Routes>
    </Stack>
  );
}
