import { BrowserRouter, Routes, Route } from "react-router-dom"
import { AppShell } from "@/components/layout/AppShell"
import { ToastProvider } from "@/components/toast-provider"
import { Home } from "@/pages/Home"
import { Browser } from "@/pages/Browser"
import { Tasks } from "@/pages/Tasks"
import { Wallets } from "@/pages/Wallets"
import { Memory } from "@/pages/Memory"
import { Reports } from "@/pages/Reports"
import { Logs } from "@/pages/Logs"
import { Settings } from "@/pages/Settings"
import { Plugins } from "@/pages/Plugins"

export default function App() {
  return (
    <ToastProvider>
      <BrowserRouter>
        <Routes>
          <Route element={<AppShell />}>
            <Route index element={<Home />} />
            <Route path="browser" element={<Browser />} />
            <Route path="tasks" element={<Tasks />} />
            <Route path="wallets" element={<Wallets />} />
            <Route path="memory" element={<Memory />} />
            <Route path="reports" element={<Reports />} />
            <Route path="logs" element={<Logs />} />
            <Route path="settings" element={<Settings />} />
            <Route path="plugins" element={<Plugins />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </ToastProvider>
  )
}
