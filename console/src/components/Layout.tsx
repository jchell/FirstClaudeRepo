import { AppShell, Badge, Burger, Group, Menu, NavLink, ScrollArea, Text, UnstyledButton } from '@mantine/core';
import { useDisclosure } from '@mantine/hooks';
import { IconChevronDown, IconLogout } from '@tabler/icons-react';
import { NavLink as RouterLink, Outlet, useLocation, useNavigate } from 'react-router-dom';

import { useAuth } from '../auth/AuthProvider';
import { logout } from '../auth/login';
import { PAGES } from '../nav';

export function Layout() {
  const [opened, { toggle, close }] = useDisclosure();
  const { user } = useAuth();
  const location = useLocation();
  const navigate = useNavigate();

  const visible = PAGES.filter(
    (p) => !p.roles?.length || user?.roles.includes('admin') || p.roles.some((r) => user?.roles.includes(r)),
  );

  return (
    <AppShell header={{ height: 56 }} navbar={{ width: 250, breakpoint: 'sm', collapsed: { mobile: !opened } }} padding="md">
      <AppShell.Header>
        <Group h="100%" px="md" justify="space-between">
          <Group gap="sm">
            <Burger opened={opened} onClick={toggle} hiddenFrom="sm" size="sm" aria-label="Toggle navigation" />
            <Text fw={700}>Data Platform</Text>
          </Group>
          <Menu position="bottom-end">
            <Menu.Target>
              <UnstyledButton aria-label="Account menu">
                <Group gap={6}>
                  <Text size="sm">{user?.username}</Text>
                  <IconChevronDown size={14} />
                </Group>
              </UnstyledButton>
            </Menu.Target>
            <Menu.Dropdown>
              <Menu.Label>Roles: {user?.roles.join(', ') || 'none'}</Menu.Label>
              <Menu.Item
                leftSection={<IconLogout size={16} />}
                onClick={async () => {
                  await logout();
                  navigate('/login', { replace: true });
                }}
              >
                Sign out
              </Menu.Item>
            </Menu.Dropdown>
          </Menu>
        </Group>
      </AppShell.Header>

      <AppShell.Navbar p="xs" aria-label="Main navigation">
        <ScrollArea>
          {visible.map((p) => {
            const active = p.path === '/' ? location.pathname === '/' : location.pathname.startsWith(p.path);
            return (
              <NavLink
                key={p.path}
                component={RouterLink}
                to={p.path}
                label={p.label}
                leftSection={<p.icon size={18} stroke={1.6} />}
                rightSection={
                  p.phase !== '0' ? (
                    <Badge size="xs" variant="light" color="gray">
                      P{p.phase}
                    </Badge>
                  ) : undefined
                }
                active={active}
                onClick={close}
              />
            );
          })}
        </ScrollArea>
      </AppShell.Navbar>

      <AppShell.Main>
        <Outlet />
      </AppShell.Main>
    </AppShell>
  );
}
