import { useAuth } from "react-oidc-context";
import { BrowserRouter, Route, Routes } from "react-router-dom";
import { Alert, Button, Center, Loader, Stack, Text, Title } from "@mantine/core";

import { AppConfig, cognitoLogoutUrl } from "./config";
import { Layout } from "./Layout";
import { SearchPage } from "./SearchPage";
import { ResultsPage } from "./ResultsPage";

function AppRoutes({
  config,
  token,
  onLogout,
}: {
  config: AppConfig;
  token: string | undefined;
  onLogout?: () => void;
}) {
  return (
    <BrowserRouter>
      <Routes>
        <Route element={<Layout config={config} token={token} onLogout={onLogout} />}>
          <Route index element={<SearchPage />} />
          <Route path="jobs/:jobId" element={<ResultsPage />} />
        </Route>
      </Routes>
    </BrowserRouter>
  );
}

/** Wraps the routes with Cognito sign-in when auth is enabled. */
function AuthedApp({ config }: { config: AppConfig }) {
  const auth = useAuth();

  if (auth.isLoading) {
    return <Center h="100vh"><Loader /></Center>;
  }
  if (auth.error) {
    return <Center h="100vh"><Alert color="red" title="Sign-in error">{auth.error.message}</Alert></Center>;
  }
  if (!auth.isAuthenticated) {
    return (
      <Center h="100vh">
        <Stack align="center">
          <Title order={2}>Survey Art</Title>
          <Text c="dimmed">Please sign in to continue.</Text>
          <Button onClick={() => auth.signinRedirect()}>Sign in</Button>
        </Stack>
      </Center>
    );
  }
  return (
    <AppRoutes
      config={config}
      token={auth.user?.access_token}
      onLogout={() => auth.removeUser().then(() => {
        window.location.href = cognitoLogoutUrl(config.cognito);
      })}
    />
  );
}

export function App({ config }: { config: AppConfig }) {
  return config.authDisabled ? (
    <AppRoutes config={config} token={undefined} />
  ) : (
    <AuthedApp config={config} />
  );
}
