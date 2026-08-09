import { Route, Routes, useLocation } from "react-router-dom";
import { AppHeader } from "./components/layout/AppHeader";
import { AppFooter } from "./components/layout/AppFooter";
import { WelcomePage } from "./pages/WelcomePage";
import { CaseCatalogPage } from "./pages/CaseCatalogPage";
import { CaseIntroductionPage } from "./pages/CaseIntroductionPage";
import { InterviewPage } from "./pages/InterviewPage";
import { InterviewCompletePage } from "./pages/InterviewCompletePage";
import { AssessmentLoadingPage } from "./pages/AssessmentLoadingPage";
import { AssessmentReviewPage } from "./pages/AssessmentReviewPage";
import { LoginPage } from "./pages/auth/LoginPage";
import { RegisterPage } from "./pages/auth/RegisterPage";
import { StudentDashboardPage } from "./pages/student/StudentDashboardPage";
import { StudentSessionPage } from "./pages/student/StudentSessionPage";
import { StudentLayout } from "./pages/student/StudentLayout";
import { StudentCasesPage } from "./pages/student/StudentCasesPage";
import { StudentAssessmentsPage } from "./pages/student/StudentAssessmentsPage";
import { StudentActivityPage } from "./pages/student/StudentActivityPage";
import { StudentProfilePage } from "./pages/student/StudentProfilePage";
import { AdminLayout } from "./pages/admin/AdminLayout";
import { AdminDashboardPage } from "./pages/admin/AdminDashboardPage";
import { AdminStudentsPage } from "./pages/admin/AdminStudentsPage";
import { AdminStudentDetailPage } from "./pages/admin/AdminStudentDetailPage";
import { AdminSessionsListPage } from "./pages/admin/AdminSessionsListPage";
import { AdminSessionPage } from "./pages/admin/AdminSessionPage";
import { AdminAuditLogPage } from "./pages/admin/AdminAuditLogPage";
import { AdminTranscriptsPage } from "./pages/admin/AdminTranscriptsPage";
import { AdminAssessmentsPage } from "./pages/admin/AdminAssessmentsPage";
import { AdminArchivedPage } from "./pages/admin/AdminArchivedPage";
import { AdminProfilePage } from "./pages/admin/AdminProfilePage";
import { SystemDashboardPage } from "./pages/admin/system/SystemDashboardPage";
import { TrafficDashboardPage } from "./pages/admin/system/TrafficDashboardPage";
import { LoadCapacityTestingPage } from "./pages/admin/system/LoadCapacityTestingPage";
import { AdminUsersPage } from "./pages/admin/AdminUsersPage";
import { AiConfigurationPage } from "./pages/admin/system/AiConfigurationPage";
import { PatientVoicesPage } from "./pages/admin/system/PatientVoicesPage";
import { AiUsageCostPage } from "./pages/admin/system/AiUsageCostPage";
import { ApiCredentialsPage } from "./pages/admin/system/ApiCredentialsPage";
import { SystemHealthPage } from "./pages/admin/system/subpages";
import { ProtectedRoute } from "./portal/ProtectedRoute";

function App() {
  const location = useLocation();
  const isHomePage = location.pathname === "/";
  const isAdminArea = location.pathname.startsWith("/admin");

  return (
    <>
      {!isAdminArea && <AppHeader />}
      <Routes>
        {/* Existing patient interview workflow (unchanged) */}
        <Route path="/" element={<WelcomePage />} />
        <Route path="/cases" element={<CaseCatalogPage />} />
        <Route path="/cases/:caseId" element={<CaseIntroductionPage />} />
        <Route path="/interview/complete" element={<InterviewCompletePage />} />
        <Route path="/interview/:caseId" element={<InterviewPage />} />
        <Route path="/assessment/:sessionId/loading" element={<AssessmentLoadingPage />} />
        <Route path="/assessment/:sessionId" element={<AssessmentReviewPage />} />

        {/* Authentication */}
        <Route path="/login" element={<LoginPage />} />
        <Route path="/register" element={<RegisterPage />} />

        {/* Patient Simulator portal. Open to any authenticated account: students
            for coursework, and promoted admins/professors for practice. The
            backend still enforces per-session ownership, so users only ever see
            their own sessions/assessments here. */}
        <Route
          element={
            <ProtectedRoute>
              <StudentLayout />
            </ProtectedRoute>
          }
        >
          <Route path="/student/dashboard" element={<StudentDashboardPage />} />
          <Route path="/student/cases" element={<StudentCasesPage />} />
          <Route path="/student/assessments" element={<StudentAssessmentsPage />} />
          <Route path="/student/activity" element={<StudentActivityPage />} />
          <Route path="/student/profile" element={<StudentProfilePage />} />
          <Route path="/student/sessions/:sessionId" element={<StudentSessionPage />} />
          <Route
            path="/student/sessions/:sessionId/assessment"
            element={<StudentSessionPage initialTab="assessment" />}
          />
        </Route>

        {/* Admin panel */}
        <Route
          path="/admin"
          element={
            <ProtectedRoute role="admin">
              <AdminLayout />
            </ProtectedRoute>
          }
        >
          <Route index element={<AdminDashboardPage />} />
          <Route path="students" element={<AdminStudentsPage />} />
          <Route path="students/:studentId" element={<AdminStudentDetailPage />} />
          <Route path="sessions" element={<AdminSessionsListPage />} />
          <Route path="sessions/:sessionId" element={<AdminSessionPage />} />
          <Route path="transcripts" element={<AdminTranscriptsPage />} />
          <Route path="assessments" element={<AdminAssessmentsPage />} />
          <Route path="users" element={<AdminUsersPage />} />
          <Route path="archived" element={<AdminArchivedPage />} />
          <Route path="profile" element={<AdminProfilePage />} />
          <Route path="audit-log" element={<AdminAuditLogPage />} />

          {/* Technical / system administration (separate from the academic dashboard) */}
          <Route path="system" element={<SystemDashboardPage />} />
          <Route path="system/traffic" element={<TrafficDashboardPage />} />
          <Route
            path="system/load-testing"
            element={<LoadCapacityTestingPage />}
          />
          <Route path="system/usage" element={<AiUsageCostPage />} />
          <Route path="system/voices" element={<PatientVoicesPage />} />
          <Route path="system/config" element={<AiConfigurationPage />} />
          <Route path="system/credentials" element={<ApiCredentialsPage />} />
          <Route path="system/health" element={<SystemHealthPage />} />
        </Route>
      </Routes>
      {!isHomePage && !isAdminArea && <AppFooter />}
    </>
  );
}

export default App;
