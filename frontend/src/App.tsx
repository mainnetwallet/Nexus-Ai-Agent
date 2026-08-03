import { BrowserRouter, Routes, Route } from "react-router-dom"
import { AppShell } from "@/components/layout/AppShell"
import { ToastProvider } from "@/components/toast-provider"
import { Home } from "@/pages/Home"
import { Chat } from "@/pages/Chat"
import { Agent } from "@/pages/Agent"
import { Browser } from "@/pages/Browser"
import { Tasks } from "@/pages/Tasks"
import { Wallets } from "@/pages/Wallets"
import { Profiles } from "@/pages/Profiles"
import { Memory } from "@/pages/Memory"
import { Reports } from "@/pages/Reports"
import { Logs } from "@/pages/Logs"
import { Settings } from "@/pages/Settings"
import { Plugins } from "@/pages/Plugins"
import { Mcp } from "@/pages/Mcp"
import { Skills } from "@/pages/Skills"
import { System } from "@/pages/System"
import { AiModels } from "@/pages/AiModels"

export default function App() {
  return (
    <ToastProvider>
      <BrowserRouter>
        <Routes>
          <Route element={<AppShell />}>
            <Route index element={<Home />} />
            <Route path="chat" element={<Chat />} />
            <Route path="agent" element={<Agent />} />
            <Route path="browser" element={<Browser />} />
            <Route path="tasks" element={<Tasks />} />
            <Route path="wallets" element={<Wallets />} />
            <Route path="profiles" element={<Profiles />} />
            <Route path="memory" element={<Memory />} />
            <Route path="reports" element={<Reports />} />
            <Route path="logs" element={<Logs />} />
            <Route path="settings" element={<Settings />} />
            <Route path="plugins" element={<Plugins />} />
            <Route path="mcp" element={<Mcp />} />
            <Route path="skills" element={<Skills />} />
            <Route path="system" element={<System />} />
            <Route path="ai-models" element={<AiModels />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </ToastProvider>
  )
}
