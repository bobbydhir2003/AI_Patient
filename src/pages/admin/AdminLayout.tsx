import { useEffect, useState } from "react";
import { Outlet, useLocation } from "react-router-dom";
import { ToastProvider } from "../../portal/ui";
import { AdminSidebar } from "../../components/admin/AdminSidebar";
import { AdminTopbar } from "../../components/admin/AdminTopbar";
import { AdminDashboardProvider } from "./AdminDashboardContext";

export function AdminLayout() {
  // Mobile navigation drawer state. On desktop the sidebar is always visible;
  // below ~900px it becomes an off-canvas drawer toggled by the hamburger.
  const [navOpen, setNavOpen] = useState(false);
  const location = useLocation();

  // Close the drawer whenever the route changes (a nav item was tapped).
  useEffect(() => {
    setNavOpen(false);
  }, [location.pathname]);

  // Prevent background scroll while the drawer is open on mobile.
  useEffect(() => {
    if (!navOpen) return;
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = prev;
    };
  }, [navOpen]);

  return (
    <ToastProvider>
      <AdminDashboardProvider>
        <div className={`pt-admin ${navOpen ? "nav-open" : ""}`}>
          <AdminTopbar onToggleNav={() => setNavOpen((v) => !v)} navOpen={navOpen} />
          <div className="pt-admin-main">
            <AdminSidebar open={navOpen} onClose={() => setNavOpen(false)} />
            <main className="pt-content" id="admin-main">
              <Outlet />
            </main>
          </div>
        </div>
      </AdminDashboardProvider>
    </ToastProvider>
  );
}
