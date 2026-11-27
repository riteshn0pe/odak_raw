// Last line of defence for render/commit crashes. A reconciliation failure
// (e.g. React's "removeChild" NotFoundError) or a thrown render blanks the whole
// app with the default red error screen. Catch it, offer a reload, and keep the
// session instead of losing the user. Minimal on purpose — no dependency, and
// only state the error so a recovered render can return to children.
import { Component, type ErrorInfo, type ReactNode } from "react";

interface Props {
  children: ReactNode;
}

interface State {
  error: Error | null;
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // no-console is not enabled in the ESLint config, so no suppression is
    // needed here — an error boundary is exactly where a console.error belongs.
    console.error("ErrorBoundary caught", error, info);
  }

  render(): ReactNode {
    if (!this.state.error) return this.props.children;
    return (
      <div className="min-h-screen grid place-items-center bg-canvas text-ink p-6">
        <div className="max-w-md text-center space-y-3">
          <h1 className="text-lg font-semibold">Something went wrong</h1>
          <p className="text-sm text-muted">
            An unexpected error occurred. Your work is safe — reload to continue.
          </p>
          <button
            className="px-4 py-2 rounded-lg bg-brand text-primary-fg text-sm font-medium cursor-pointer"
            onClick={() => window.location.reload()}
          >
            Reload
          </button>
        </div>
      </div>
    );
  }
}
