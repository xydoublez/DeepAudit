/**
 * SSO Callback Page — 处理后端 Casdoor SSO 回调
 *
 * 后端完成 OAuth 授权码 + PKCE 流程后，将本地令牌通过 URL fragment 回传：
 *   /sso/callback#access_token=xxx&token_type=bearer
 * 出错时回传：
 *   /sso/callback#error=xxx
 *
 * 本页解析 fragment、存储令牌后整页跳转到首页，由 AuthProvider 重新校验会话，
 * 避免 SPA 内部认证状态的时序竞态。
 */

import { useEffect, useRef, useState } from "react";

export default function SsoCallback() {
  const [status, setStatus] = useState<"processing" | "error">("processing");
  const handled = useRef(false);

  useEffect(() => {
    // 防止 React StrictMode 下的重复执行
    if (handled.current) return;
    handled.current = true;

    // 令牌经 URL fragment 回传（fragment 不进入服务器日志与 Referer）
    const hash = window.location.hash.replace(/^#/, "");
    const fragParams = new URLSearchParams(hash);
    const queryParams = new URLSearchParams(window.location.search);

    const error = fragParams.get("error") || queryParams.get("error");
    const accessToken =
      fragParams.get("access_token") || queryParams.get("access_token");

    if (error) {
      setStatus("error");
      const msg = decodeURIComponent(error);
      // 跳回登录页并由其统一展示错误提示
      window.location.replace(`/login?error=${encodeURIComponent(msg)}`);
      return;
    }

    if (!accessToken) {
      setStatus("error");
      window.location.replace(
        `/login?error=${encodeURIComponent("登录失败：未收到访问令牌")}`
      );
      return;
    }

    // 存储令牌 + SSO 会话标记（登出时据此跳转 Casdoor 统一登出）
    sessionStorage.setItem("access_token", accessToken);
    sessionStorage.setItem("auth_provider", "sso");
    // 登录成功：清除自动跳转冷却标记，恢复登录页自动直通能力
    sessionStorage.removeItem("sso_auto_redirect_at");

    // 整页加载首页，由 AuthProvider.checkAuth 校验令牌并建立会话
    window.location.replace("/");
  }, []);

  return (
    <div className="min-h-screen flex items-center justify-center cyber-bg-elevated relative overflow-hidden">
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

      <div
        className="cyber-dialog border border-border/60 rounded-lg px-10 py-8 text-center relative z-10"
        style={{ boxShadow: "0 4px 30px rgba(0,0,0,0.5)" }}
      >
        {status === "error" ? (
          <div className="flex flex-col items-center gap-3">
            <span className="w-8 h-8 border-2 border-red-500/40 border-t-red-500 rounded-full animate-spin" />
            <p className="font-mono text-sm text-muted-foreground">
              正在返回登录页...
            </p>
          </div>
        ) : (
          <div className="flex flex-col items-center gap-3">
            <span className="w-8 h-8 border-2 border-primary/30 border-t-primary rounded-full animate-spin" />
            <p className="font-mono text-sm text-muted-foreground tracking-wider">
              // 正在完成单点登录...
            </p>
          </div>
        )}
      </div>
    </div>
  );
}
