import { Navigate, Route, Routes } from "react-router-dom";
import { Layout } from "./components/Layout";
import { ChatPage } from "./pages/ChatPage";
import { DashboardPage } from "./pages/DashboardPage";
import { HomePage } from "./pages/HomePage";
import { NewProjectPage } from "./pages/NewProjectPage";
import { ProjectRunPage } from "./pages/ProjectRunPage";
import { TokenUsagePage } from "./pages/TokenUsagePage";

export default function App() {
  return (
    <Layout>
      <Routes>
        <Route path="/" element={<HomePage />} />
        <Route path="/projects/new" element={<NewProjectPage />} />
        <Route path="/tokens" element={<TokenUsagePage />} />
        <Route path="/projects/:projectId/run" element={<ProjectRunPage />} />
        <Route path="/projects/:projectId" element={<DashboardPage />} />
        <Route path="/projects/:projectId/chat" element={<ChatPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </Layout>
  );
}
