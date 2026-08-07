import { useCallback, useEffect, useRef, useState } from "react";
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

  // 正在播放的 section key
  const [playingKey, setPlayingKey] = useState<string | null>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);

  const seenRef = useRef<Set<string>>(new Set());
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // ---- 播放逻辑 ----

  const playWithChatTTS = useCallback(
    async (s: TelegramSection): Promise<void> => {
      const key = sectionKey(s);
      setPlayingKey(key);

      // 取消之前的音频
      if (audioRef.current) {
        audioRef.current.pause();
        audioRef.current = null;
      }

      const text = buildSpeechText(s);
      const seed = voice.seed!;

      try {
        const params = new URLSearchParams({ text, seed: String(seed) });
        const audioUrl = `/api/live-telegram/audio?${params}`;
        const resp = await fetch(audioUrl);
        if (!resp.ok) throw new Error(`${resp.status}`);

        const blob = await resp.blob();
        const url = URL.createObjectURL(blob);
        const audio = new Audio(url);
        audioRef.current = audio;

        audio.onended = () => {
          setPlayingKey(null);
          URL.revokeObjectURL(url);
          audioRef.current = null;
        };
        audio.onerror = () => {
          setPlayingKey(null);
          URL.revokeObjectURL(url);
          audioRef.current = null;
        };

        await audio.play();
      } catch (e) {
        console.error("ChatTTS 播放失败:", e);
        setPlayingKey(null);
      }
    },
    [voice],
  );

  const playWithWebSpeech = useCallback(
    (s: TelegramSection): void => {
      const key = sectionKey(s);
      setPlayingKey(key);

      window.speechSynthesis?.cancel();

      const text = buildSpeechText(s);
      const utt = new SpeechSynthesisUtterance(text);
      utt.lang = "zh-CN";
      utt.rate = 1.1;
      utt.pitch = 1.0;
      utt.volume = 0.9;
      utt.onend = () => setPlayingKey(null);
      utt.onerror = () => setPlayingKey(null);

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
    const interval = isMarketOpen() ? 5000 : 15000;
    intervalRef.current = setInterval(fetchData, interval);
    return () => {
      if (intervalRef.current) clearInterval(intervalRef.current);
    };
  }, [fetchData]);

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
        <div className="relative">
          <button
            onClick={() => setVoiceMenuOpen((v) => !v)}
            className="flex items-center gap-1.5 rounded-btn border border-border bg-elevated px-2.5 py-1.5 text-xs text-secondary hover:text-foreground transition-colors cursor-pointer"
          >
            <Volume2 className="h-3 w-3 text-accent/70" />
            <span className="max-w-[120px] truncate hidden sm:inline">
              {voice.label}
            </span>
            <ChevronDown className="h-3 w-3 opacity-50" />
          </button>
          {/* 下拉菜单 — click 展开，z-50 确保不被内容遮挡 */}
          {voiceMenuOpen && (
            <>
              <div
                className="fixed inset-0 z-40"
                onClick={() => setVoiceMenuOpen(false)}
              />
              <div className="absolute right-0 top-full mt-1 w-52 rounded-card border border-border bg-surface shadow-2xl z-50">
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
            </>
          )}
        </div>

        {/* 最新时间 */}
        {lastTime && (
          <div className="hidden sm:flex items-center gap-1.5 font-mono text-xs text-muted">
            <Wifi className="h-3 w-3 text-accent/60" />
            <span>最新 {lastTime}</span>
          </div>
        )}
      </header>

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
                    className={`shrink-0 flex items-center justify-center w-6 h-6 rounded-md transition-all cursor-pointer ${
                      isPlaying
                        ? "bg-accent/20 text-accent"
                        : "text-muted hover:text-accent hover:bg-accent/10"
                    }`}
                    title="朗读本条"
                  >
                    {isPlaying ? (
                      <Loader2 className="h-3.5 w-3.5 animate-spin" />
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
