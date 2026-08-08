import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { motion, AnimatePresence } from "framer-motion";
import {
  Volume2,
  Radio,
  Wifi,
  Clock,
  TrendingUp,
  Play,
  Loader2,
  ChevronDown,
} from "lucide-react";

// ---- types ----

interface TelegramItem {
  emoji: string;
  label: string;
  text: string;
}

interface TelegramSection {
  time: string;
  title: string;
  items: TelegramItem[];
}

interface TelegramResponse {
  sections: TelegramSection[];
  file: string | null;
  updated_at: string | null;
  total: number;
}

// ---- header data types ----

interface HeaderConcept {
  name: string;
  direction: string;     // ▲加速 / ▼衰减 / →维持 / 🆕新出
  recent_activity: number;
}

interface HeaderStock {
  name: string;
  state: string;          // 观察 / 关注 / 待确认 / 确认
  concepts: string[];
  consecutive_minutes: number;
  return_pct: number | null;
  anchor: string | null;
}

interface HeaderMarketSummary {
  main_theme: string;
  key_stocks: string[];
  limit_up_count: number;
  limit_down_count: number;
}

interface HeaderState {
  hot_concepts: HeaderConcept[];
  watchlist: HeaderStock[];
  market_summary: HeaderMarketSummary;
  updated_at: string | null;
}

interface VoiceOption {
  engine: "chattts" | "web-speech";
  seed?: number;
  label: string;
}

// ---- voice options ----

const VOICE_OPTIONS: VoiceOption[] = [
  { engine: "chattts", seed: 2, label: "ChatTTS · 沉稳男声" },
  { engine: "chattts", seed: 6616, label: "ChatTTS · 年轻女声" },
  { engine: "chattts", seed: 1111, label: "ChatTTS · 磁性女声" },
  { engine: "web-speech", label: "Web Speech · 浏览器" },
];

// ---- helpers ----

function sectionKey(s: TelegramSection): string {
  return `${s.time}|${s.title}|${s.items.length}`;
}

function isMarketOpen(): boolean {
  const now = new Date();
  const t = now.getHours() * 60 + now.getMinutes();
  return t >= 9 * 60 + 30 && t <= 15 * 60 + 30;
}

function buildSpeechText(s: TelegramSection): string {
  const parts = s.items
    .filter((it) => it.label && it.text)
    .map((it) => `${it.label}：${it.text}`);
  if (parts.length === 0) return s.title;
  const raw = `${s.title}。${parts.join("。")}`;
  return raw.length > 300 ? raw.slice(0, 280) + "等。" : raw;
}

// ---- color map ----

const LABEL_COLORS: Record<string, string> = {
  "涨停": "text-bull",
  "跌停": "text-bear",
  "热点概念": "text-amber-400",
  "相似度": "text-sky-400",
  "影响": "text-emerald-400",
  "证据": "text-violet-400",
  "操作": "text-accent",
  "股票": "text-cyan-400",
  "资金": "text-rose-400",
  "数据状态": "text-warning",
  "信号": "text-accent",
};

const LABEL_BG: Record<string, string> = {
  "涨停": "bg-bull/10 border-bull/20",
  "跌停": "bg-bear/10 border-bear/20",
  "热点概念": "bg-amber-400/10 border-amber-400/20",
  "相似度": "bg-sky-400/10 border-sky-400/20",
  "影响": "bg-emerald-400/10 border-emerald-400/20",
  "证据": "bg-violet-400/10 border-violet-400/20",
  "操作": "bg-accent/10 border-accent/20",
  "股票": "bg-cyan-400/10 border-cyan-400/20",
  "资金": "bg-rose-400/10 border-rose-400/20",
  "数据状态": "bg-warning/10 border-warning/20",
  "信号": "bg-accent/10 border-accent/20",
};

function itemColor(label: string): string {
  for (const [k, v] of Object.entries(LABEL_COLORS)) {
    if (label.includes(k)) return v;
  }
  return "text-secondary";
}

function itemBg(label: string): string {
  for (const [k, v] of Object.entries(LABEL_BG)) {
    if (label.includes(k)) return v;
  }
  return "bg-elevated/60 border-border/40";
}

// ---- component ----

export default function LiveFeed() {
  const [sections, setSections] = useState<TelegramSection[]>([]);
  const [newIds, setNewIds] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);

  // 语音引擎
  const [voiceIdx, setVoiceIdx] = useState(() => {
    try {
      return parseInt(localStorage.getItem("livefeed-voice") || "0", 10);
    } catch {
      return 0;
    }
  });
  const voice = VOICE_OPTIONS[voiceIdx] || VOICE_OPTIONS[0];
  const [voiceMenuOpen, setVoiceMenuOpen] = useState(false);
  const voiceBtnRef = useRef<HTMLButtonElement>(null);
  const [voiceMenuPos, setVoiceMenuPos] = useState({ top: 0, left: 0 });

  // 正在播放的 section key + 片段进度
  const [playingKey, setPlayingKey] = useState<string | null>(null);
  const [clipProgress, setClipProgress] = useState("");
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const abortRef = useRef(false);

  const seenRef = useRef<Set<string>>(new Set());
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // ---- 顶部持久信息栏 ----

  const [header, setHeader] = useState<HeaderState>({
    hot_concepts: [],
    watchlist: [],
    market_summary: {
      main_theme: "",
      key_stocks: [],
      limit_up_count: 0,
      limit_down_count: 0,
    },
    updated_at: null,
  });

  const fetchHeader = useCallback(async () => {
    try {
      const resp = await fetch("/api/live-telegram/header");
      if (!resp.ok) return;
      const data: HeaderState = await resp.json();
      setHeader(data);
    } catch {
      /* header fetch is best-effort */
    }
  }, []);

  // ---- 流式播放（ChatTTS 分句） ----

  const queueRef = useRef<{ url: string; index: number; total: number }[]>([]);
  const playingRef = useRef(false);

  const pumpQueue = useCallback(async () => {
    if (playingRef.current) return;
    playingRef.current = true;

    while (!abortRef.current) {
      const clip = queueRef.current.shift();
      if (!clip) { playingRef.current = false; break; }

      setClipProgress(`${clip.index + 1}/${clip.total}`);
      const audio = new Audio(clip.url);
      audioRef.current = audio;

      try {
        await new Promise<void>((resolve) => {
          audio.onended = () => resolve();
          audio.onerror = () => resolve();
          audio.play().catch(() => resolve());
        });
      } catch { /* ignore */ }
    }
    playingRef.current = false;
  }, []);

  const playWithChatTTS = useCallback(
    async (s: TelegramSection): Promise<void> => {
      const key = sectionKey(s);
      setPlayingKey(key);
      setClipProgress("...");
      abortRef.current = true;
      if (audioRef.current) { audioRef.current.pause(); audioRef.current = null; }
      queueRef.current = [];

      const text = buildSpeechText(s);
      const seed = voice.seed!;

      try {
        const params = new URLSearchParams({ text, seed: String(seed), stream: "true" });
        const resp = await fetch(`/api/live-telegram/clips?${params}`, { method: "POST" });
        if (!resp.ok) throw new Error(`${resp.status}`);

        const reader = resp.body?.getReader();
        if (!reader) throw new Error("no stream");
        const decoder = new TextDecoder();
        abortRef.current = false;

        // 读取第一句后立即开始播放
        let buf = "";
        while (!abortRef.current) {
          const { done, value } = await reader.read();
          if (done) break;
          buf += decoder.decode(value, { stream: true });

          const lines = buf.split("\n");
          buf = lines.pop() || "";  // 保留未完成的行

          for (const line of lines) {
            if (!line.trim() || abortRef.current) continue;
            try {
              const clip = JSON.parse(line);
              queueRef.current.push(clip);
              pumpQueue();  // 非阻塞启动播放
            } catch { /* skip */ }
          }
        }
      } catch (e) {
        console.error("ChatTTS 播放失败:", e);
      }
      setPlayingKey(null);
      setClipProgress("");
    },
    [voice, pumpQueue],
  );

  const playWithWebSpeech = useCallback(
    (s: TelegramSection): void => {
      const key = sectionKey(s);
      setPlayingKey(key);
      setClipProgress("");
      abortRef.current = true;
      if (audioRef.current) { audioRef.current.pause(); audioRef.current = null; }
      window.speechSynthesis?.cancel();

      const text = buildSpeechText(s);
      const utt = new SpeechSynthesisUtterance(text);
      utt.lang = "zh-CN";
      utt.rate = 1.1;
      utt.pitch = 1.0;
      utt.volume = 0.9;
      utt.onend = () => { setPlayingKey(null); setClipProgress(""); };
      utt.onerror = () => { setPlayingKey(null); setClipProgress(""); };
      window.speechSynthesis?.speak(utt);
    },
    [],
  );

  const playSection = useCallback(
    (s: TelegramSection) => {
      if (voice.engine === "chattts") {
        playWithChatTTS(s);
      } else {
        playWithWebSpeech(s);
      }
    },
    [voice, playWithChatTTS, playWithWebSpeech],
  );

  // ---- 数据轮询 ----

  const fetchData = useCallback(async () => {
    try {
      const resp = await fetch("/api/live-telegram?limit=30");
      if (!resp.ok) throw new Error(`${resp.status}`);
      const data: TelegramResponse = await resp.json();
      setError(null);

      const incoming = data.sections;
      if (incoming.length === 0) return;

      const freshKeys = new Set<string>();
      for (const s of incoming) {
        const k = sectionKey(s);
        if (!seenRef.current.has(k)) {
          freshKeys.add(k);
        }
      }

      seenRef.current = new Set(incoming.map(sectionKey));

      if (freshKeys.size > 0 && sections.length > 0) {
        setNewIds(freshKeys);
        setTimeout(() => setNewIds(new Set()), 4000);
      }

      setSections(incoming);
    } catch (e: any) {
      setError(e.message || "加载失败");
    }
  }, [sections.length]);

  useEffect(() => {
    fetchData();
    fetchHeader();
    const interval = isMarketOpen() ? 5000 : 15000;
    intervalRef.current = setInterval(fetchData, interval);
    // Header refreshes every 12s (2-3× the feed interval)
    const headerInterval = setInterval(fetchHeader, 12000);
    return () => {
      if (intervalRef.current) clearInterval(intervalRef.current);
      clearInterval(headerInterval);
    };
  }, [fetchData, fetchHeader]);

  useEffect(() => {
    const check = setInterval(() => {
      if (intervalRef.current) {
        clearInterval(intervalRef.current);
        const interval = isMarketOpen() ? 5000 : 15000;
        intervalRef.current = setInterval(fetchData, interval);
      }
    }, 30000);
    return () => clearInterval(check);
  }, [fetchData]);

  const handleVoiceChange = (idx: number) => {
    setVoiceIdx(idx);
    try {
      localStorage.setItem("livefeed-voice", String(idx));
    } catch {
      /* ignore */
    }
    // 切换引擎时停止当前播放
    window.speechSynthesis?.cancel();
    if (audioRef.current) {
      audioRef.current.pause();
      audioRef.current = null;
    }
    setPlayingKey(null);
  };

  const live = isMarketOpen();
  const lastTime = sections.length > 0 ? sections[0].time : null;

  return (
    <div className="flex flex-col h-full">
      {/* ---- 顶部栏 ---- */}
      <header className="shrink-0 border-b border-border bg-surface/80 backdrop-blur px-4 py-2.5 flex items-center gap-3 flex-wrap">
        <div className="flex items-center gap-2">
          <Radio className="h-5 w-5 text-accent" />
          <h1 className="text-lg font-semibold text-foreground tracking-wide">
            实况
          </h1>
          {live && (
            <span className="inline-flex items-center gap-1 rounded-full bg-bull/15 px-2 py-0.5 text-[10px] font-semibold text-bull">
              <span className="h-1.5 w-1.5 rounded-full bg-bull animate-pulse" />
              直播中
            </span>
          )}
          {!live && (
            <span className="inline-flex items-center gap-1 rounded-full bg-muted/20 px-2 py-0.5 text-[10px] text-muted">
              <Clock className="h-3 w-3" />
              休市
            </span>
          )}
        </div>

        <div className="flex-1" />

        {/* 语音引擎选择 */}
        <button
          ref={voiceBtnRef}
          onClick={() => {
            const rect = voiceBtnRef.current?.getBoundingClientRect();
            if (rect) {
              setVoiceMenuPos({ top: rect.bottom + 4, left: rect.right - 208 });
            }
            setVoiceMenuOpen((v) => !v);
          }}
          className="flex items-center gap-1.5 rounded-btn border border-border bg-elevated px-2.5 py-1.5 text-xs text-secondary hover:text-foreground transition-colors cursor-pointer"
        >
          <Volume2 className="h-3 w-3 text-accent/70" />
          <span className="max-w-[120px] truncate hidden sm:inline">
            {voice.label}
          </span>
          <ChevronDown className="h-3 w-3 opacity-50" />
        </button>
        {/* 下拉菜单 — portal 到 body，避免被父容器 overflow 裁切 */}
        {voiceMenuOpen &&
          createPortal(
            <>
              <div
                className="fixed inset-0 z-[9998]"
                onClick={() => setVoiceMenuOpen(false)}
              />
              <div
                className="fixed z-[9999] w-52 rounded-card border border-border bg-surface shadow-2xl"
                style={{ top: voiceMenuPos.top, left: voiceMenuPos.left }}
              >
                {VOICE_OPTIONS.map((opt, i) => (
                  <button
                    key={i}
                    onClick={() => {
                      handleVoiceChange(i);
                      setVoiceMenuOpen(false);
                    }}
                    className={`w-full text-left px-3 py-2 text-xs transition-colors first:rounded-t-card last:rounded-b-card cursor-pointer ${
                      i === voiceIdx
                        ? "bg-accent/10 text-accent font-medium"
                        : "text-secondary hover:bg-elevated hover:text-foreground"
                    }`}
                  >
                    <div className="flex items-center gap-2">
                      {i === voiceIdx && (
                        <span className="h-1.5 w-1.5 rounded-full bg-accent" />
                      )}
                      <span className={i !== voiceIdx ? "ml-[14px]" : ""}>
                        {opt.label}
                      </span>
                    </div>
                  </button>
                ))}
              </div>
            </>,
            document.body,
          )}

        {/* 最新时间 */}
        {lastTime && (
          <div className="hidden sm:flex items-center gap-1.5 font-mono text-xs text-muted">
            <Wifi className="h-3 w-3 text-accent/60" />
            <span>最新 {lastTime}</span>
          </div>
        )}
      </header>

      {/* ---- 持久信息栏：热门概念 + 重点观察 + 市场主线 ---- */}
      {(header.hot_concepts.length > 0 || header.watchlist.length > 0) && (
        <div className="shrink-0 border-b border-border bg-elevated/30 px-4 py-2 space-y-1.5">
          {/* Row 1: 热门概念 + 方向信号 */}
          {header.hot_concepts.length > 0 && (
            <div className="flex items-center gap-1.5 flex-wrap">
              <span className="text-[10px] font-semibold text-amber-400/70 uppercase tracking-wide shrink-0">
                热门概念
              </span>
              {header.hot_concepts.slice(0, 6).map((c) => {
                const dirClass =
                  c.direction === "▲加速"
                    ? "text-emerald-400"
                    : c.direction === "▼衰减"
                      ? "text-rose-400"
                      : c.direction === "🆕新出"
                        ? "text-sky-400"
                        : "text-slate-400";
                return (
                  <span
                    key={c.name}
                    className="inline-flex items-center gap-0.5 rounded px-1.5 py-0.5 text-[11px] bg-surface border border-border/60"
                  >
                    <span className={dirClass}>{c.direction}</span>
                    <span className="text-foreground/80">{c.name}</span>
                  </span>
                );
              })}
            </div>
          )}

          {/* Row 2: 重点观察个股 + 市场纵览 */}
          <div className="flex items-center gap-3 flex-wrap">
            {header.watchlist.length > 0 && (
              <div className="flex items-center gap-1.5 flex-wrap">
                <span className="text-[10px] font-semibold text-cyan-400/70 uppercase tracking-wide shrink-0">
                  重点观察
                </span>
                {header.watchlist.slice(0, 5).map((s) => {
                  const stateColor =
                    s.state === "确认"
                      ? "text-emerald-400 bg-emerald-500/15"
                      : s.state === "待确认"
                        ? "text-sky-400 bg-sky-500/15"
                        : s.state === "关注"
                          ? "text-amber-400 bg-amber-500/15"
                          : "text-slate-400 bg-slate-500/10";
                  const pct =
                    s.return_pct != null
                      ? (s.return_pct >= 0 ? "+" : "") + s.return_pct.toFixed(1) + "%"
                      : "";
                  return (
                    <span
                      key={s.name}
                      className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] bg-surface border border-border/60"
                      title={`${s.concepts.slice(0, 2).join("、")} · 连续${s.consecutive_minutes}分钟${s.anchor ? ` · 锚点${s.anchor}` : ""}`}
                    >
                      <span className="text-foreground/80 font-medium">
                        {s.name}
                      </span>
                      <span className={`rounded-sm px-0.5 text-[9px] font-semibold ${stateColor}`}>
                        {s.state}
                      </span>
                      {pct && (
                        <span
                          className={`text-[10px] tabular-nums ${
                            s.return_pct != null && s.return_pct >= 0
                              ? "text-bull/70"
                              : "text-bear/70"
                          }`}
                        >
                          {pct}
                        </span>
                      )}
                    </span>
                  );
                })}
              </div>
            )}

            {/* Market quick stats */}
            <div className="flex items-center gap-2 text-[10px] text-muted/70 ml-auto">
              {header.market_summary.main_theme && (
                <span className="hidden sm:inline text-secondary/70 truncate max-w-[200px]">
                  {header.market_summary.main_theme}
                </span>
              )}
              {header.market_summary.limit_up_count > 0 && (
                <span className="text-bull/70 font-mono tabular-nums">
                  涨停{header.market_summary.limit_up_count}
                </span>
              )}
              {header.market_summary.limit_down_count > 0 && (
                <span className="text-bear/70 font-mono tabular-nums">
                  跌停{header.market_summary.limit_down_count}
                </span>
              )}
            </div>
          </div>
        </div>
      )}

      {/* ---- 内容区 ---- */}
      <div className="flex-1 min-h-0 overflow-y-auto px-4 py-3 space-y-2.5">
        {error && (
          <div className="rounded-card border border-danger/30 bg-danger/10 px-4 py-2 text-xs text-danger">
            加载失败：{error}
            <button
              onClick={fetchData}
              className="ml-3 underline hover:text-danger/80"
            >
              重试
            </button>
          </div>
        )}

        {sections.length === 0 && !error && (
          <div className="flex flex-col items-center justify-center py-20 text-muted gap-3">
            <Radio className="h-10 w-10 opacity-30" />
            <p className="text-sm">等待电报数据…</p>
            <p className="text-xs text-muted/60">
              交易时段每分钟自动更新
            </p>
          </div>
        )}

        <AnimatePresence initial={false}>
          {sections.map((s) => {
            const key = sectionKey(s);
            const isNew = newIds.has(key);
            const isPlaying = playingKey === key;

            return (
              <motion.div
                key={key}
                initial={
                  isNew
                    ? { opacity: 0, y: -20, scale: 0.97 }
                    : false
                }
                animate={{ opacity: 1, y: 0, scale: 1 }}
                transition={{
                  duration: 0.35,
                  ease: [0.16, 1, 0.3, 1],
                }}
                className={`rounded-card border bg-surface/60 backdrop-blur overflow-hidden ${
                  isNew
                    ? "border-accent/40 shadow-[0_0_20px_rgba(59,130,246,0.12)]"
                    : "border-border hover:border-border/80"
                } transition-colors duration-300`}
              >
                {/* Section 标题行 */}
                <div className="flex items-center gap-2 px-4 py-2.5 border-b border-border/60 bg-elevated/40">
                  <span className="font-mono text-xs font-semibold text-accent tabular">
                    {s.time || "--:--"}
                  </span>
                  <span className="text-xs text-secondary truncate flex-1">
                    {s.title}
                  </span>
                  {isNew && (
                    <span className="shrink-0 rounded bg-accent/15 px-1.5 py-px text-[9px] font-bold text-accent animate-pulse">
                      NEW
                    </span>
                  )}
                  {/* 播放按钮 */}
                  <button
                    onClick={() => playSection(s)}
                    disabled={isPlaying}
                    className={`shrink-0 flex items-center gap-1 rounded-md px-1.5 py-0.5 transition-all cursor-pointer ${
                      isPlaying
                        ? "bg-accent/20 text-accent"
                        : "text-muted hover:text-accent hover:bg-accent/10"
                    }`}
                    title="朗读本条"
                  >
                    {isPlaying ? (
                      <>
                        <Loader2 className="h-3 w-3 animate-spin" />
                        {clipProgress && (
                          <span className="text-[9px] font-mono text-accent/70">{clipProgress}</span>
                        )}
                      </>
                    ) : (
                      <Play className="h-3.5 w-3.5" />
                    )}
                  </button>
                </div>

                {/* Items */}
                <div className="px-4 py-2.5 space-y-1.5">
                  {s.items.map((item, i) => (
                    <div
                      key={i}
                      className={`flex items-start gap-2 rounded-md border px-2.5 py-1.5 text-xs leading-relaxed ${itemBg(
                        item.label,
                      )}`}
                    >
                      {item.emoji && (
                        <span className="shrink-0 text-sm leading-none mt-px">
                          {item.emoji}
                        </span>
                      )}
                      {item.label && (
                        <span
                          className={`shrink-0 font-semibold ${itemColor(
                            item.label,
                          )}`}
                        >
                          {item.label}
                        </span>
                      )}
                      <span className="text-foreground/85 break-all">
                        {item.text}
                      </span>
                    </div>
                  ))}
                </div>
              </motion.div>
            );
          })}
        </AnimatePresence>

        {sections.length > 0 && (
          <div className="flex items-center justify-center gap-1.5 py-3 text-[10px] text-muted/50">
            <TrendingUp className="h-3 w-3" />
            <span>
              共 {sections.length} 条 · 最新优先
              {live ? " · 每 5 秒刷新" : " · 每 15 秒刷新"}
            </span>
          </div>
        )}
      </div>
    </div>
  );
}
