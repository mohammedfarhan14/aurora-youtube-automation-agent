import React, { Component, type ErrorInfo, type ReactNode } from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "./styles.css";

type ErrorBoundaryState = { hasError: boolean };

class AppErrorBoundary extends Component<
  { children: ReactNode },
  ErrorBoundaryState
> {
  state: ErrorBoundaryState = { hasError: false };

  static getDerivedStateFromError(): ErrorBoundaryState {
    return { hasError: true };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("Aurora Studio failed to render.", error, info.componentStack);
  }

  render() {
    if (this.state.hasError) {
      return (
        <main className="loading-screen" role="alert">
          <h1>Aurora Studio could not load</h1>
          <p>Please reload the page. If the problem continues, check the browser console.</p>
          <button className="button button-primary" onClick={() => window.location.reload()}>
            Reload Aurora Studio
          </button>
        </main>
      );
    }

    return this.props.children;
  }
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <AppErrorBoundary>
      <App />
    </AppErrorBoundary>
  </React.StrictMode>,
);
