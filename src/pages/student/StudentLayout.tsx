import { Outlet } from "react-router-dom";
import { MobileBottomNav } from "../../components/mobile/MobileBottomNav";

/** Shared shell for the student patient app: renders the active page and the
 * mobile bottom navigation. The wrapper class reserves space so the fixed nav
 * never covers content on phones (see `.pt-has-bottomnav` in portal.css). */
export function StudentLayout() {
  return (
    <div className="pt-has-bottomnav">
      <Outlet />
      <MobileBottomNav />
    </div>
  );
}
