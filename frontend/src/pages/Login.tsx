/**
 * Login Page — Casdoor SSO 单点登录入口
 * Cyberpunk Terminal Aesthetic
 *
 * 仅 SSO 登录：SSO 启用时自动顶层导航到后端 /api/v1/auth/sso/login（门户已登录且
 * 开启 Auto Signin 时全程免点击直通），并保留手动按钮兜底；
 * 由后端代理完成 OAuth 2.0 授权码 + PKCE 流程，回调后携带本地令牌返回前端。
 */

import { useState, useEffect } from "react";
import { useNavigate, useLocation } from "react-router-dom";
import { useAuth } from "@/shared/context/AuthContext";
import { apiClient } from "@/shared/api/serverClient";
import { Button } from "@/components/ui/button";
import { toast } from "sonner";
import { Terminal, Shield, Fingerprint, KeyRound, AlertTriangle } from "lucide-react";
import { version } from "../../package.json";

// 后端 SSO 登录入口（相对前端域，经 nginx / vite 代理到后端）
const API_BASE = import.meta.env.VITE_API_BASE_URL || "/api/v1";
const SSO_LOGIN_URL = `${API_BASE}/auth/sso/login`;

// 自动跳转冷却标记：回调持续失败时防止登录页↔门户无限循环跳转
const SSO_AUTO_REDIRECT_KEY = "sso_auto_redirect_at";
const AUTO_REDIRECT_COOLDOWN_MS = 60_000;

export default function Login() {
  const [loading, setLoading] = useState(false);
  // null=检测中, true=已启用, false=未启用
  const [ssoEnabled, setSsoEnabled] = useState<boolean | null>(null);
  const [hasError, setHasError] = useState(false);
  const [autoRedirecting, setAutoRedirecting] = useState(false);
  const [cooldownActive, setCooldownActive] = useState(false);
  const navigate = useNavigate();
  const location = useLocation();
  const { isAuthenticated } = useAuth();

  const from = location.state?.from?.pathname || "/";

  // 已登录则直接跳转到目标页
  useEffect(() => {
    if (isAuthenticated && !loading) {
      navigate(from, { replace: true });
    }
  }, [isAuthenticated, navigate, from, loading]);

  // 查询后端 SSO 是否启用
  useEffect(() => {
    const checkSso = async () => {
      try {
        const res = await apiClient.get("/auth/sso/config");
        setSsoEnabled(Boolean(res.data?.enabled));
      } catch {
        setSsoEnabled(false);
      }
    };
    checkSso();
  }, []);

  // 展示回调/跳转带回的错误信息（?error=xxx）
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const err = params.get("error");
    if (err) {
      setHasError(true);
      toast.error(decodeURIComponent(err));
      window.history.replaceState({}, "", "/login");
    }
  }, []);

  // SSO 启用后自动发起授权跳转：门户会话存在且开启 Auto Signin 时免点击直通
  // 防循环：冷却期内已自动跳转并回到登录页、或本次携带错误时不再自动跳转
  useEffect(() => {
    if (ssoEnabled !== true || hasError) return;
    const last = Number(sessionStorage.getItem(SSO_AUTO_REDIRECT_KEY) || 0);
    if (Date.now() - last < AUTO_REDIRECT_COOLDOWN_MS) {
      setCooldownActive(true);
      return;
    }
    sessionStorage.setItem(SSO_AUTO_REDIRECT_KEY, String(Date.now()));
    setAutoRedirecting(true);
    window.location.href = SSO_LOGIN_URL;
  }, [ssoEnabled, hasError]);

  const handleSsoLogin = () => {
    setLoading(true);
    // 顶层导航到后端 SSO 入口（后端 302 → Casdoor 授权端点）
    window.location.href = SSO_LOGIN_URL;
  };

  return (
    <div className="min-h-screen flex items-center justify-center cyber-bg-elevated relative overflow-hidden">
      {/* Scanline overlay */}
      <div className="absolute inset-0 pointer-events-none z-20">
        <div
          className="absolute inset-0"
          style={{
            backgroundImage: "repeating-linear-gradient(0deg, transparent, transparent 2px, rgba(0,0,0,0.1) 2px, rgba(0,0,0,0.1) 4px)",
          }}
        />
      </div>

      {/* Vignette effect */}
      <div
        className="absolute inset-0 pointer-events-none z-10"
        style={{
          background: "radial-gradient(ellipse at center, transparent 0%, transparent 50%, rgba(0,0,0,0.5) 100%)",
        }}
      />

      {/* Grid background */}
      <div
        className="absolute inset-0 opacity-[0.03]"
        style={{
          backgroundImage: `
            linear-gradient(rgba(255,107,44,0.5) 1px, transparent 1px),
            linear-gradient(90deg, rgba(255,107,44,0.5) 1px, transparent 1px)
          `,
          backgroundSize: "32px 32px",
        }}
      />

      {/* Corner Decorations */}
      <div className="absolute top-4 left-4 text-sm font-mono text-muted-foreground z-30 space-y-1">
        <div className="flex items-center gap-2">
          <Terminal className="w-4 h-4" />
          <span>SYS_ID: 0x84F2</span>
        </div>
        <div className="flex items-center gap-2">
          <Shield className="w-4 h-4" />
          <span>ENCRYPT: AES-256</span>
        </div>
        <div className="flex items-center gap-2">
          <Fingerprint className="w-4 h-4" />
          <span>SSO: CASDOOR</span>
        </div>
      </div>

      <div className="absolute top-4 right-4 text-sm font-mono text-muted-foreground text-right z-30 space-y-1">
        <div>SECURE_CONN: TRUE</div>
        <div>PORT: 443</div>
        <div>TLS: 1.3</div>
      </div>

      <div className="absolute bottom-4 left-4 text-sm font-mono text-muted-foreground z-30">
        DEEPAUDIT_AUTH_v3
      </div>

      <div className="absolute bottom-4 right-4 text-sm font-mono text-muted-foreground z-30">
        {new Date().toISOString().split("T")[0]}
      </div>

      {/* Main Card */}
      <div className="w-full max-w-md relative z-30 px-4">
        {/* Logo & Title */}
        <div className="text-center mb-8">
          <div className="inline-flex items-center justify-center p-3 cyber-dialog border border-border/60 rounded-lg mb-6"
               style={{ boxShadow: '0 0 30px rgba(255,107,44,0.1)' }}>
            <img
              src="/logo_deepaudit.png"
              alt="DeepAudit"
              className="w-14 h-14 object-contain"
            />
          </div>
          <div
            className="text-3xl font-bold tracking-wider mb-2 font-mono"
            style={{ textShadow: "0 0 30px rgba(255,107,44,0.5), 0 0 60px rgba(255,107,44,0.3)" }}
          >
            <span className="text-primary">DEEP</span>
            <span className="text-foreground">AUDIT</span>
          </div>
          <p className="text-base font-mono text-muted-foreground">
            // Autonomous Security Agent
          </p>
        </div>

        {/* SSO Login Card */}
        <div className="cyber-dialog border border-border/60 rounded-lg overflow-hidden"
             style={{ boxShadow: '0 4px 30px rgba(0,0,0,0.5)' }}>
          {/* Card Header */}
          <div className="flex items-center gap-2 px-4 py-3 cyber-bg-elevated border-b border-border">
            <div className="flex items-center gap-1.5">
              <div className="w-3 h-3 rounded-full bg-red-500/80" />
              <div className="w-3 h-3 rounded-full bg-yellow-500/80" />
              <div className="w-3 h-3 rounded-full bg-green-500/80" />
            </div>
            <span className="ml-2 font-mono text-sm text-muted-foreground tracking-wider">
              authentication@deepaudit
            </span>
          </div>

          <div className="p-6">
            <div className="flex flex-col gap-5">
              <div className="text-center space-y-2">
                <div className="inline-flex items-center justify-center w-16 h-16 rounded-full border border-primary/30 cyber-bg-elevated">
                  <KeyRound className="w-8 h-8 text-primary" />
                </div>
                <p className="font-mono text-sm text-muted-foreground">
                  // 通过统一身份认证平台登录
                </p>
              </div>

              {ssoEnabled === false && (
                <div className="flex items-start gap-2 p-3 rounded border border-yellow-500/40 bg-yellow-500/10">
                  <AlertTriangle className="w-4 h-4 text-yellow-500 mt-0.5 shrink-0" />
                  <p className="font-mono text-xs text-yellow-500/90 leading-relaxed">
                    单点登录未启用，请联系管理员在后端配置 CASDOOR_ENABLED 及应用凭证。
                  </p>
                </div>
              )}

              <Button
                type="button"
                onClick={handleSsoLogin}
                disabled={loading || autoRedirecting || ssoEnabled === null || ssoEnabled === false}
                className="w-full h-12 text-base font-bold uppercase tracking-wider bg-primary hover:bg-primary/90 text-foreground border border-primary/50 transition-all"
                style={{ boxShadow: '0 0 20px rgba(255,107,44,0.3)' }}
              >
                {loading || autoRedirecting ? (
                  <span className="flex items-center gap-2">
                    <span className="w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin" />
                    正在跳转统一认证...
                  </span>
                ) : ssoEnabled === null ? (
                  <span className="flex items-center gap-2">
                    <span className="w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin" />
                    检测中...
                  </span>
                ) : (
                  <span className="flex items-center gap-2">
                    <Fingerprint className="w-5 h-5" />
                    使用 SSO 单点登录
                  </span>
                )}
              </Button>

              {(cooldownActive || hasError) && (
                <p className="text-center font-mono text-xs text-muted-foreground">
                  // 自动跳转已暂停，请手动点击登录
                </p>
              )}
            </div>

            {/* Footer */}
            <div className="mt-6 pt-5 border-t border-border text-center">
              <p className="text-base font-mono text-muted-foreground">
                OAuth 2.0 · PKCE · OpenID Connect
              </p>
            </div>
          </div>
        </div>

        {/* Version Info */}
        <div className="mt-6 text-center">
          <p className="font-mono text-sm text-muted-foreground uppercase">
            Version {version} · Secure Connection
          </p>
        </div>
      </div>
    </div>
  );
}
