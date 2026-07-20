import { Outlet } from "react-router-dom";
import { ToastProvider } from "../../portal/ui";
import { AdminSidebar } from "../../components/admin/AdminSidebar";
import { AdminTopbar } from "../../components/admin/AdminTopbar";
import { AdminDashboardProvider } from "./AdminDashboardContext";

export function AdminLayout() {
  return (
    <ToastProvider>
      <AdminDashboardProvider>
        <div className="pt-admin">
          <AdminTopbar />
          <div className="pt-admin-main">
            <AdminSidebar />
            <main className="pt-content" id="admin-main">
              <Outlet />
            </main>
          </div>
        </div>
      </AdminDashboardProvider>
    </ToastProvider>
  );
}
