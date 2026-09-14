import React from "react";
import ReactDOM from "react-dom/client";
import { MantineProvider } from "@mantine/core";
import { AuthProvider } from "react-oidc-context";
import { WebStorageStateStore } from "oidc-client-ts";
import "@mantine/core/styles.css";

import { loadConfig, AppConfig } from "./config";
import { App } from "./App";

function render(config: AppConfig) {
  const root = ReactDOM.createRoot(document.getElementById("root")!);

  // Auth disabled (local dev): render the app directly with no Cognito provider.
  if (config.authDisabled) {
    root.render(
      <React.StrictMode>
        <MantineProvider>
          <App config={config} />
        </MantineProvider>
      </React.StrictMode>,
    );
    return;
  }

  const oidcConfig = {
    authority: config.cognito.authority,
    client_id: config.cognito.clientId,
    redirect_uri: window.location.origin + "/",
    post_logout_redirect_uri: window.location.origin + "/",
    response_type: "code",
    scope: config.cognito.scope,
    userStore: new WebStorageStateStore({ store: window.localStorage }),
    onSigninCallback: () => {
      window.history.replaceState({}, document.title, window.location.pathname);
    },
  };

  root.render(
    <React.StrictMode>
      <MantineProvider>
        <AuthProvider {...oidcConfig}>
          <App config={config} />
        </AuthProvider>
      </MantineProvider>
    </React.StrictMode>,
  );
}

loadConfig().then(render);
