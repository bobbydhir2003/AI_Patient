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

        {/* Student portal */}
        <Route
          path="/student/dashboard"
          element={
            <ProtectedRoute role="student">
              <StudentDashboardPage />
            </ProtectedRoute>
          }
        />
        <Route
          path="/student/sessions/:sessionId"
          element={
            <ProtectedRoute role="student">
              <StudentSessionPage />
            </ProtectedRoute>
          }
        />
        <Route
          path="/student/sessions/:sessionId/assessment"
          element={
            <ProtectedRoute role="student">
              <StudentSessionPage initialTab="assessment" />
            </ProtectedRoute>
          }
        />

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
            element={
              <ProtectedRoute role="super_admin">
                <LoadCapacityTestingPage />
              </ProtectedRoute>
            }
          />
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
