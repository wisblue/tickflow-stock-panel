import { useCallback, useEffect, useRef, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import {
  Volume2,
  VolumeX,
  Radio,
  Wifi,
  Clock,
  TrendingUp,
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

// ---- helpers ----

function sectionKey(s: TelegramSection): string {
  return `${s.time}|${s.title}|${s.items.length}`;
}

/** 判断当前是否在 A 股交易时段内 */
function isMarketOpen(): boolean {
  const now = new Date();
  const h = now.getHours();
  const m = now.getMinutes();
  const t = h * 60 + m;
  return t >= 9 * 60 + 30 && t <= 15 * 60 + 30;
}

/** 从 section items 中提取语音播报文本 */
function buildSpeechText(s: TelegramSection): string {
  const parts = s.items
    .filter((it) => it.label && it.text)
    .map((it) => `${it.label}：${it.text}`);
  if (parts.length === 0) return s.title;
  return `${s.title}。${parts.join("。")}`;
}

// ---- color map by label ----

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
  const [muted, setMuted] = useState(() => {
    try {
      return localStorage.getItem("livefeed-muted") === "1";
    } catch {
      return false;
    }
  });
  const [updatedAt, setUpdatedAt] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const seenRef = useRef<Set<string>>(new Set());
  const containerRef = useRef<HTMLDivElement>(null);
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const speak = useCallback(
    (s: TelegramSection) => {
      if (muted) return;
      if (!window.speechSynthesis) return;

      // 取消当前正在播放的语音
      window.speechSynthesis.cancel();

      const text = buildSpeechText(s);
      if (text.length > 400) {
        // 太长则截断
        const short = text.slice(0, 380) + "等。";
        const utt = new SpeechSynthesisUtterance(short);
        utt.lang = "zh-CN";
        utt.rate = 1.1;
        utt.pitch = 1.0;
        utt.volume = 0.9;
        window.speechSynthesis.speak(utt);
      } else {
        const utt = new SpeechSynthesisUtterance(text);
        utt.lang = "zh-CN";
        utt.rate = 1.1;
        utt.pitch = 1.0;
        utt.volume = 0.9;
        window.speechSynthesis.speak(utt);
      }
    },
    [muted],
  );

  const fetchData = useCallback(async () => {
    try {
      const resp = await fetch("/api/live-telegram?limit=30");
      if (!resp.ok) throw new Error(`${resp.status}`);
      const data: TelegramResponse = await resp.json();
      setUpdatedAt(data.updated_at);
      setError(null);

      const incoming = data.sections;
      if (incoming.length === 0) return;

      // 检测新 section
      const fresh: TelegramSection[] = [];
      const freshKeys = new Set<string>();
      for (const s of incoming) {
        const k = sectionKey(s);
        if (!seenRef.current.has(k)) {
          fresh.push(s);
          freshKeys.add(k);
        }
      }

      // 更新已见集合
      const allKeys = new Set(incoming.map(sectionKey));
      seenRef.current = allKeys;

      if (fresh.length > 0 && sections.length > 0) {
        // 有新数据 — 标记新条目用于动画，并播报
        setNewIds(freshKeys);
        // 播报最新的一条
        if (fresh.length > 0) {
          speak(fresh[0]);
        }
        // 清除新条目标记（3 秒后）
        setTimeout(() => setNewIds(new Set()), 3000);
      }

      setSections(incoming);

      // 首次加载也播报最新条目
      if (sections.length === 0 && fresh.length > 0) {
        // 首次加载不播报，避免刷屏
      }
    } catch (e: any) {
      setError(e.message || "加载失败");
    }
  }, [sections.length, speak]);

  // 轮询
  useEffect(() => {
    fetchData();
    const interval = isMarketOpen() ? 5000 : 15000;
    intervalRef.current = setInterval(fetchData, interval);
    return () => {
      if (intervalRef.current) clearInterval(intervalRef.current);
    };
  }, [fetchData]);

  // 在交易/非交易时段间动态调整轮询间隔
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

  const toggleMute = () => {
    const next = !muted;
    setMuted(next);
    try {
      localStorage.setItem("livefeed-muted", next ? "1" : "0");
    } catch {
      /* ignore */
    }
    if (next) {
      window.speechSynthesis?.cancel();
    }
  };

  const live = isMarketOpen();
  const lastTime =
    sections.length > 0 ? sections[0].time : null;

  return (
    <div ref={containerRef} className="flex flex-col h-full">
      {/* ---- 顶部栏 ---- */}
      <header className="shrink-0 border-b border-border bg-surface/80 backdrop-blur px-6 py-3 flex items-center gap-3">
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

        {/* 最新时间 */}
        {lastTime && (
          <div className="hidden sm:flex items-center gap-1.5 font-mono text-xs text-muted">
            <Wifi className="h-3 w-3 text-accent/60" />
            <span>最新 {lastTime}</span>
            {updatedAt && (
              <span className="text-[10px] text-muted/50">
                (更新于{" "}
                {new Date(updatedAt).toLocaleTimeString("zh-CN", {
                  hour: "2-digit",
                  minute: "2-digit",
                  second: "2-digit",
                })}
                )
              </span>
            )}
          </div>
        )}

        {/* 静音按钮 */}
        <button
          onClick={toggleMute}
          className={`flex items-center gap-1.5 rounded-btn px-3 py-1.5 text-xs font-medium transition-all duration-200 cursor-pointer ${
            muted
              ? "bg-elevated text-muted hover:text-secondary border border-border"
              : "bg-accent/15 text-accent border border-accent/30 shadow-[0_0_12px_rgba(59,130,246,0.15)]"
          }`}
          title={muted ? "取消静音" : "静音"}
        >
          {muted ? (
            <VolumeX className="h-3.5 w-3.5" />
          ) : (
            <Volume2 className="h-3.5 w-3.5" />
          )}
          <span className="hidden sm:inline">
            {muted ? "已静音" : "播报中"}
          </span>
        </button>
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
            const isNew = newIds.has(sectionKey(s));
            return (
              <motion.div
                key={sectionKey(s)}
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
                  <span className="text-xs text-secondary truncate">
                    {s.title}
                  </span>
                  {isNew && (
                    <span className="shrink-0 rounded bg-accent/15 px-1.5 py-px text-[9px] font-bold text-accent animate-pulse">
                      NEW
                    </span>
                  )}
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

        {/* 底部指示 */}
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
