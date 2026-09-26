import '@mantine/core/styles.css';
import '@mantine/notifications/styles.css';

import { lazy, StrictMode, Suspense } from 'react';
import { createRoot } from 'react-dom/client';
import { MantineProvider } from '@mantine/core';
import { Notifications } from '@mantine/notifications';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { BrowserRouter, Route, Routes } from 'react-router-dom';

import { AuthProvider, RequireAuth } from './auth/AuthProvider';
import { Layout } from './components/Layout';
import { PAGES } from './nav';
import { AdminPage } from './pages/admin/AdminPage';
import { HomePage } from './pages/HomePage';
import { LoginPage } from './pages/LoginPage';
import { PlaceholderPage } from './pages/PlaceholderPage';
import { CatalogPage, DatasetPage } from './pages/catalog/CatalogPage';
import { ConnectionsPage } from './pages/connections/ConnectionsPage';
import { JobDetailPage, JobsPage } from './pages/ingestion/JobsPage';
import { JobWizard } from './pages/ingestion/JobWizard';
import { PortalPage } from './pages/portal/PortalPage';

// The lineage graph libraries are large; load them only when lineage is opened.
const LineagePage = lazy(() => import('./pages/lineage/LineagePage').then((m) => ({ default: m.LineagePage })));

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: 1, staleTime: 5_000 } } });

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <MantineProvider defaultColorScheme="auto">
      <Notifications />
      <QueryClientProvider client={queryClient}>
        <BrowserRouter>
          <AuthProvider>
            <Routes>
              <Route path="/login" element={<LoginPage />} />
              <Route
                element={
                  <RequireAuth>
                    <Layout />
                  </RequireAuth>
                }
              >
                <Route index element={<HomePage />} />
                <Route
                  path="admin/*"
                  element={
                    <RequireAuth roles={['admin']}>
                      <AdminPage />
                    </RequireAuth>
                  }
                />
                <Route path="connections" element={<ConnectionsPage />} />
                <Route path="ingestion" element={<JobsPage />} />
                <Route path="ingestion/new" element={<RequireAuth roles={['engineer']}><JobWizard /></RequireAuth>} />
                <Route path="ingestion/:jobId" element={<JobDetailPage />} />
                <Route path="ingestion/:jobId/edit" element={<RequireAuth roles={['engineer']}><JobWizard /></RequireAuth>} />
                <Route path="catalog" element={<CatalogPage />} />
                <Route path="catalog/:datasetId" element={<DatasetPage />} />
                <Route path="lineage" element={<Suspense fallback={null}><LineagePage /></Suspense>} />
                <Route path="portal" element={<PortalPage />} />
                {PAGES.filter((p) => !p.ready).map((p) => (
                  <Route key={p.path} path={p.path.slice(1)} element={<PlaceholderPage page={p} />} />
                ))}
                <Route path="*" element={<HomePage />} />
              </Route>
            </Routes>
          </AuthProvider>
        </BrowserRouter>
      </QueryClientProvider>
    </MantineProvider>
  </StrictMode>,
);
