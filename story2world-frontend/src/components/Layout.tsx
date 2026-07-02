import type { ReactNode } from "react";
import { Link, NavLink } from "react-router-dom";
import { LanguageSwitcher, useLanguage } from "../i18n";
import { BookIcon, SparkIcon } from "./Icons";

export function Layout({ children }: { children: ReactNode }) {
  const { t } = useLanguage();
  return (
    <div className="app-shell">
      <header className="site-header">
        <Link className="brand" to="/">
          <span className="brand-mark">
            <BookIcon />
          </span>
          <span>
            <strong>Story2World</strong>
            <small>Author World Engine</small>
          </span>
        </Link>
        <nav>
          <NavLink to="/" end>
            {t("nav.home")}
          </NavLink>
          <NavLink to="/projects/new">{t("nav.newWorld")}</NavLink>
          <NavLink to="/tokens">{t("nav.tokens")}</NavLink>
        </nav>
        <LanguageSwitcher />
        <span className="engine-badge"><SparkIcon /> AUTHOR MODEL</span>
      </header>
      <main>{children}</main>
      <footer>
        <span>Story2World Interactive Fiction System</span>
        <span className="footer-dot" />
        <span>{t("footer.tagline")}</span>
      </footer>
    </div>
  );
}
