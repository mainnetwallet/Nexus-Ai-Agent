import { useEffect, useRef, useState } from "react"
import {
  MessageSquare,
  Send,
  Trash2,
  Download,
  Bot,
  User,
  Globe,
  MonitorPlay,
  Activity,
  ScrollText,
  Loader2,
} from "lucide-react"
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Textarea } from "@/components/ui/textarea"
import { useAsync } from "@/hooks/use-async"
import { api, type ChatCategory, type ChatMessage } from "@/lib/api"

const CHAT_SESSION_STORAGE_KEY = "nexus.chat.session_id"

const CATEGORY_VARIANT: Record<ChatCategory, "neutral" | "amber" | "cyan" | "green" | "red" | "violet"> = {
  conversation: "neutral",
  question: "cyan",
  browser_command: "violet",
  agent_command: "amber",
  task: "green",
  settings: "neutral",
  system_request: "cyan",
}

async function getOrCreateSessionId(): Promise<string> {
  const stored = localStorage.getItem(CHAT_SESSION_STORAGE_KEY)
  if (stored) return stored
  const session = await api.chat.createSession("dashboard")
  localStorage.setItem(CHAT_SESSION_STORAGE_KEY, session.id)
  return session.id
}

export function Chat() {
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [input, setInput] = useState("")
  const [sending, setSending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const bottomRef = useRef<HTMLDivElement>(null)

  const agent = useAsync(() => api.agent.status(), [])
  const browserStatus = useAsync(() => api.browser.status(), [])

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const id = await getOrCreateSessionId()
        if (cancelled) return
        setSessionId(id)
        const history = await api.chat.getMessages(id)
        if (cancelled) return
        setMessages(history)
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err))
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  useEffect(() => {
    const id = setInterval(() => {
      agent.refetch()
      browserStatus.refetch()
    }, 4000)
    return () => clearInterval(id)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: "end", behavior: "smooth" })
  }, [messages])

  async function send() {
    const text = input.trim()
    if (!text || !sessionId || sending) return
    setInput("")
    setSending(true)
    setError(null)

    const optimisticUser: ChatMessage = {
      id: `pending-${Date.now()}`,
      role: "user",
      content: text,
      category: null,
      meta: {},
      created_at: new Date().toISOString(),
    }
    setMessages((prev) => [...prev, optimisticUser])

    try {
      const result = await api.chat.sendMessage(sessionId, text)
      const assistantMessage: ChatMessage = {
        id: `${optimisticUser.id}-reply`,
        role: "assistant",
        content: result.reply,
        category: result.category,
        meta: result.meta,
        created_at: new Date().toISOString(),
      }
      setMessages((prev) => [...prev, assistantMessage])
      agent.refetch()
      browserStatus.refetch()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setSending(false)
    }
  }

  async function clearChat() {
    if (!sessionId) return
    await api.chat.clearMessages(sessionId)
    setMessages([])
  }

  async function exportChat() {
    if (!sessionId) return
    const data = await api.chat.exportMessages(sessionId)
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" })
    const url = URL.createObjectURL(blob)
    const a = document.createElement("a")
    a.href = url
    a.download = `nexus-chat-${sessionId.slice(0, 8)}.json`
    a.click()
    URL.revokeObjectURL(url)
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault()
      send()
    }
  }

  const agentData = agent.data
  const browserData = browserStatus.data

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-center justify-between">
        <div>
          <p className="font-mono text-xs uppercase tracking-[0.2em] text-[var(--color-signal-cyan)]">
            Conversational interface
          </p>
          <h1 className="mt-1 text-2xl font-semibold text-[var(--color-text)]">AI Chat</h1>
          <p className="mt-1 text-sm text-[var(--color-text-muted)]">
            Talk to the agent naturally — ask questions, give it tasks, or control it directly.
          </p>
        </div>
        <div className="flex gap-2">
          <Button variant="ghost" size="sm" onClick={exportChat} disabled={!sessionId || messages.length === 0}>
            <Download className="size-4" />
            Export
          </Button>
          <Button variant="ghost" size="sm" onClick={clearChat} disabled={!sessionId || messages.length === 0}>
            <Trash2 className="size-4" />
            Clear
          </Button>
        </div>
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <Card className="lg:col-span-2 flex flex-col">
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <MessageSquare className="size-4 text-[var(--color-signal-cyan)]" />
              Conversation
            </CardTitle>
            <CardDescription>
              Try: "what are you doing?", "open example.com", "pause", "what happened today?"
            </CardDescription>
          </CardHeader>
          <CardContent className="flex flex-1 flex-col gap-3">
            {error && <p className="text-sm text-[var(--color-signal-red)]">{error}</p>}

            <div className="flex h-[420px] flex-col gap-3 overflow-y-auto rounded-md border border-[var(--color-border)] bg-[#060a12] p-4">
              {messages.length === 0 && (
                <div className="flex flex-1 flex-col items-center justify-center gap-2 text-center">
                  <MessageSquare className="size-6 text-[var(--color-text-faint)]" />
                  <p className="text-sm text-[var(--color-text-faint)]">
                    Say hello, or just tell it what to do.
                  </p>
                </div>
              )}
              {messages.map((m) => (
                <ChatBubble key={m.id} message={m} />
              ))}
              {sending && (
                <div className="flex items-center gap-2 text-xs text-[var(--color-text-faint)]">
                  <Loader2 className="size-3.5 animate-spin" />
                  thinking…
                </div>
              )}
              <div ref={bottomRef} />
            </div>

            <div className="flex items-end gap-2">
              <Textarea
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={onKeyDown}
                placeholder="Message Nexus-Agent…"
                className="min-h-[52px]"
                disabled={!sessionId}
              />
              <Button onClick={send} disabled={!sessionId || !input.trim() || sending}>
                <Send className="size-4" />
                Send
              </Button>
            </div>
          </CardContent>
        </Card>

        <div className="flex flex-col gap-4">
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <Activity className="size-4 text-[var(--color-signal-amber)]" />
                Current task
              </CardTitle>
            </CardHeader>
            <CardContent className="flex flex-col gap-2">
              {agentData?.current_task_id ? (
                <>
                  <InfoRow icon={Bot} label="Action" value={agentData.current_action ?? "—"} />
                  <InfoRow icon={Globe} label="Website" value={agentData.current_website ?? "—"} mono />
                </>
              ) : (
                <p className="text-sm text-[var(--color-text-faint)]">No task in flight.</p>
              )}
              <div className="mt-1 flex items-center gap-2">
                <span
                  className={`size-2 rounded-full ${
                    agentData?.status === "running" ? "bg-[var(--color-signal-green)]" : "bg-[var(--color-text-faint)]"
                  }`}
                />
                <span className="text-xs text-[var(--color-text-muted)]">{agentData?.status ?? "unknown"}</span>
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <MonitorPlay className="size-4 text-[var(--color-signal-cyan)]" />
                Live browser
              </CardTitle>
            </CardHeader>
            <CardContent className="flex flex-col gap-2">
              <div className="relative aspect-video overflow-hidden rounded-md border border-[var(--color-border)] bg-black">
                {browserData?.active ? (
                  <LiveScreenshot />
                ) : (
                  <div className="flex h-full w-full items-center justify-center">
                    <p className="text-xs text-[var(--color-text-faint)]">No active session</p>
                  </div>
                )}
              </div>
              <InfoRow icon={Globe} label="URL" value={browserData?.url || "—"} mono />
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <ScrollText className="size-4 text-[var(--color-text-muted)]" />
                Quick stats
              </CardTitle>
            </CardHeader>
            <CardContent className="grid grid-cols-2 gap-3">
              <InfoStat label="Completed" value={String(agentData?.tasks_completed ?? 0)} />
              <InfoStat label="Failed" value={String(agentData?.tasks_failed ?? 0)} />
            </CardContent>
          </Card>
        </div>
      </div>
    </div>
  )
}

function ChatBubble({ message }: { message: ChatMessage }) {
  const isUser = message.role === "user"
  return (
    <div className={`flex gap-2.5 ${isUser ? "flex-row-reverse" : ""}`}>
      <div
        className={`mt-0.5 flex size-6 shrink-0 items-center justify-center rounded-full ${
          isUser ? "bg-[var(--color-signal-cyan-dim)]" : "bg-[var(--color-signal-amber-dim)]"
        }`}
      >
        {isUser ? (
          <User className="size-3.5 text-[var(--color-signal-cyan)]" />
        ) : (
          <Bot className="size-3.5 text-[var(--color-signal-amber)]" />
        )}
      </div>
      <div className={`flex max-w-[80%] flex-col gap-1 ${isUser ? "items-end" : "items-start"}`}>
        <div
          className={`rounded-lg px-3 py-2 text-sm whitespace-pre-wrap ${
            isUser
              ? "bg-[var(--color-signal-cyan-dim)] text-[var(--color-text)]"
              : "bg-[var(--color-surface)] text-[var(--color-text)] border border-[var(--color-border)]"
          }`}
        >
          {message.content}
        </div>
        {message.category && (
          <Badge variant={CATEGORY_VARIANT[message.category]}>{message.category.replace("_", " ")}</Badge>
        )}
      </div>
    </div>
  )
}

function LiveScreenshot() {
  const [imgUrl, setImgUrl] = useState<string | null>(null)
  const objectUrlRef = useRef<string | null>(null)

  useEffect(() => {
    let cancelled = false
    async function poll() {
      try {
        const blob = await api.browser.screenshotBlob()
        if (cancelled || !blob) return
        const url = URL.createObjectURL(blob)
        if (objectUrlRef.current) URL.revokeObjectURL(objectUrlRef.current)
        objectUrlRef.current = url
        setImgUrl(url)
      } catch {
        // quietly retry on next tick
      }
    }
    poll()
    const id = setInterval(poll, 2000)
    return () => {
      cancelled = true
      clearInterval(id)
      if (objectUrlRef.current) URL.revokeObjectURL(objectUrlRef.current)
    }
  }, [])

  if (!imgUrl) {
    return (
      <div className="flex h-full w-full items-center justify-center">
        <p className="text-xs text-[var(--color-text-faint)]">Connecting…</p>
      </div>
    )
  }
  return <img src={imgUrl} alt="Live agent browser view" className="h-full w-full object-contain" />
}

function InfoRow({
  icon: Icon,
  label,
  value,
  mono,
}: {
  icon: typeof Globe
  label: string
  value: string
  mono?: boolean
}) {
  return (
    <div className="flex items-start gap-2.5">
      <Icon className="mt-0.5 size-4 shrink-0 text-[var(--color-text-faint)]" />
      <div className="min-w-0">
        <p className="text-xs uppercase tracking-wide text-[var(--color-text-muted)]">{label}</p>
        <p className={`truncate text-sm text-[var(--color-text)] ${mono ? "font-mono" : ""}`}>{value}</p>
      </div>
    </div>
  )
}

function InfoStat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <p className="text-xs uppercase tracking-wide text-[var(--color-text-muted)]">{label}</p>
      <p className="mt-1 font-mono text-sm text-[var(--color-text)]">{value}</p>
    </div>
  )
}
