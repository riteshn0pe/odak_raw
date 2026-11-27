// Catch-all 404. Deliberately dependency-free apart from the landing link: no
// session read, no API call, so it renders whether or not the visitor is signed
// in. "Sign in" leaves for the API-origin landing page — the SPA has no login
// route of its own.
import { Link } from "react-router-dom";
import { Button } from "../components/ui";
import { landingUrl } from "../lib/auth";

export function NotFound() {
  return (
    <div className="min-h-screen bg-canvas flex items-center justify-center px-6 py-14">
      <div className="w-full max-w-sm text-center">
        <div className="text-[88px] leading-none font-extrabold tracking-tighter text-faint select-none">404</div>

        <h1 className="text-xl font-bold tracking-tight mt-5 m-0">Page not found</h1>
        <p className="text-sm text-muted mt-2 m-0">
          The page you are looking for does not exist, or it has moved.
        </p>

        <div className="flex flex-wrap items-center justify-center gap-2 mt-7">
          <Link to="/" className="no-underline">
            <Button variant="primary">Go to dashboard</Button>
          </Link>
          <a href={landingUrl()} className="no-underline">
            <Button variant="secondary">Sign in</Button>
          </a>
        </div>
      </div>
    </div>
  );
}
