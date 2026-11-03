/**
 * Account Page
 * Cyberpunk Terminal Aesthetic
 */

import { useState, useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar";
import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from "@/components/ui/alert-dialog";
import {
  User,
  Mail,
  Phone,
  Shield,
  Calendar,
  Save,
  KeyRound,
  LogOut,
  UserPlus,
  GitBranch,
  Terminal,
  Copy,
  RefreshCw,
  Ban,
  Code2
} from "lucide-react";
import { apiClient } from "@/shared/api/serverClient";
import { useAuth } from "@/shared/context/AuthContext";
import { toast } from "sonner";
import type { Profile } from "@/shared/types";

export default function Account() {
  const navigate = useNavigate();
  const { logout } = useAuth();
  const [profile, setProfile] = useState<Profile | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [showLogoutDialog, setShowLogoutDialog] = useState(false);
  const [form, setForm] = useState({
    full_name: "",
    phone: "",
    github_username: "",
    gitlab_username: "",
  });
  const [passwordForm, setPasswordForm] = useState({
    current_password: "",
    new_password: "",
    confirm_password: "",
  });
  const [changingPassword, setChangingPassword] = useState(false);

  // API Key 状态
  interface ApiKeyInfo {
    has_api_key: boolean;
    api_key_prefix?: string;
    api_key_created_at?: string;
    api_key_expires_at?: string;
    is_expired: boolean;
  }
  const [apiKeyInfo, setApiKeyInfo] = useState<ApiKeyInfo | null>(null);
  const [newApiKey, setNewApiKey] = useState<string | null>(null);
  const [apiKeyBusy, setApiKeyBusy] = useState(false);
  const [showKeyPasswordDialog, setShowKeyPasswordDialog] = useState(false);
  const [keyPassword, setKeyPassword] = useState("");
  const [showRevokeDialog, setShowRevokeDialog] = useState(false);

  useEffect(() => {
    loadProfile();
    loadApiKeyInfo();
  }, []);

  const loadProfile = async () => {
    try {
      setLoading(true);
      const res = await apiClient.get('/users/me');
      setProfile(res.data);
      setForm({
        full_name: res.data.full_name || "",
        phone: res.data.phone || "",
        github_username: res.data.github_username || "",
        gitlab_username: res.data.gitlab_username || "",
      });
    } catch (error) {
      console.error('Failed to load profile:', error);
      toast.error("加载账号信息失败");
    } finally {
      setLoading(false);
    }
  };

  const handleSave = async () => {
    try {
      setSaving(true);
      const res = await apiClient.put('/users/me', form);
      setProfile(res.data);
      toast.success("账号信息已更新");
    } catch (error) {
      console.error('Failed to update profile:', error);
      toast.error("更新失败");
    } finally {
      setSaving(false);
    }
  };

  const handleChangePassword = async () => {
    if (!passwordForm.new_password || !passwordForm.confirm_password) {
      toast.error("请填写新密码");
      return;
    }
    if (passwordForm.new_password !== passwordForm.confirm_password) {
      toast.error("两次输入的密码不一致");
      return;
    }
    if (passwordForm.new_password.length < 6) {
      toast.error("密码长度至少6位");
      return;
    }

    try {
      setChangingPassword(true);
      await apiClient.put('/users/me', { password: passwordForm.new_password });
      toast.success("密码已更新（如有 API Key 将同步吊销）");
      setPasswordForm({ current_password: "", new_password: "", confirm_password: "" });
      await loadApiKeyInfo();
    } catch (error) {
      console.error('Failed to change password:', error);
      toast.error("密码更新失败");
    } finally {
      setChangingPassword(false);
    }
  };

  const formatDate = (dateString?: string) => {
    if (!dateString) return "-";
    return new Date(dateString).toLocaleDateString('zh-CN', {
      year: 'numeric',
      month: 'long',
      day: 'numeric'
    });
  };

  // ============ API Key 管理 ============

  const loadApiKeyInfo = async () => {
    try {
      const res = await apiClient.get('/users/me/api-key');
      setApiKeyInfo(res.data);
    } catch (error) {
      console.error('Failed to load API key info:', error);
    }
  };

  const handleGenerateApiKey = async () => {
    if (!keyPassword) {
      toast.error("请输入登录密码进行确认");
      return;
    }
    try {
      setApiKeyBusy(true);
      const res = await apiClient.post('/users/me/api-key', { password: keyPassword });
      setNewApiKey(res.data.api_key);
      await loadApiKeyInfo();
      toast.success("API Key 已生成");
    } catch (error: any) {
      const detail = error?.response?.data?.detail;
      toast.error(detail === "密码确认失败" ? "密码确认失败，请检查登录密码" : "生成 API Key 失败");
    } finally {
      setApiKeyBusy(false);
      setKeyPassword("");
      setShowKeyPasswordDialog(false);
    }
  };

  const handleRevokeApiKey = async () => {
    try {
      setApiKeyBusy(true);
      await apiClient.delete('/users/me/api-key');
      await loadApiKeyInfo();
      toast.success("API Key 已吊销");
    } catch (error) {
      console.error('Failed to revoke API key:', error);
      toast.error("吊销 API Key 失败");
    } finally {
      setApiKeyBusy(false);
      setShowRevokeDialog(false);
    }
  };

  const copyText = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      toast.success("已复制到剪贴板");
    } catch {
      toast.error("复制失败，请手动复制");
    }
  };

  const getInitials = (name?: string, email?: string) => {
    if (name) return name.charAt(0).toUpperCase();
    if (email) return email.charAt(0).toUpperCase();
    return "U";
  };

  const handleLogout = () => {
    logout(true);
    toast.success("已退出登录");
    navigate('/login');
  };

  const handleSwitchAccount = () => {
    logout(true);
    navigate('/login');
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-screen cyber-bg-elevated">
        <div className="text-center space-y-4">
          <div className="loading-spinner mx-auto" />
          <p className="text-muted-foreground font-mono text-sm uppercase tracking-wider">加载中...</p>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6 p-6 cyber-bg-elevated min-h-screen font-mono relative">
      {/* Grid background */}
      <div className="absolute inset-0 cyber-grid-subtle pointer-events-none" />

      <div className="relative z-10 grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Profile Card */}
        <div className="cyber-card p-0">
          <div className="cyber-card-header">
            <User className="w-5 h-5 text-primary" />
            <h3 className="text-lg font-bold uppercase tracking-wider text-foreground">用户信息</h3>
          </div>
          <div className="p-6 text-center">
            <div className="relative inline-block mb-4">
              <Avatar className="w-24 h-24 border-2 border-primary/30">
                <AvatarImage src={profile?.avatar_url} />
                <AvatarFallback className="bg-primary/20 text-primary text-2xl font-bold">
                  {getInitials(profile?.full_name, profile?.email)}
                </AvatarFallback>
              </Avatar>
              <div className="absolute -bottom-1 -right-1 w-6 h-6 bg-emerald-500 rounded-full border-2 border-background flex items-center justify-center">
                <div className="w-2 h-2 bg-foreground rounded-full animate-pulse" />
              </div>
            </div>
            <h4 className="text-lg font-bold text-foreground uppercase mb-1">
              {profile?.full_name || "未设置姓名"}
            </h4>
            <p className="text-muted-foreground text-sm">{profile?.email}</p>

            <div className="mt-6 pt-6 border-t border-border space-y-3 text-left">
              <div className="flex items-center gap-3 text-sm">
                <Shield className="w-4 h-4 text-violet-400" />
                <span className="text-muted-foreground">角色:</span>
                <span className="text-violet-400 font-bold uppercase">
                  {profile?.role === 'admin' ? '管理员' : '成员'}
                </span>
              </div>
              <div className="flex items-center gap-3 text-sm">
                <Calendar className="w-4 h-4 text-sky-400" />
                <span className="text-muted-foreground">注册时间:</span>
                <span className="text-foreground font-mono">{formatDate(profile?.created_at)}</span>
              </div>
            </div>

            <div className="mt-6 pt-6 border-t border-border space-y-2">
              <Button
                variant="outline"
                onClick={handleSwitchAccount}
                className="w-full cyber-btn-outline h-10"
              >
                <UserPlus className="w-4 h-4 mr-2" />
                切换账号
              </Button>
              <Button
                variant="destructive"
                onClick={() => setShowLogoutDialog(true)}
                className="w-full bg-rose-500/20 hover:bg-rose-500/30 text-rose-400 border border-rose-500/30 h-10"
              >
                <LogOut className="w-4 h-4 mr-2" />
                退出登录
              </Button>
            </div>
          </div>
        </div>

        {/* Edit Form */}
        <div className="lg:col-span-2 cyber-card p-0">
          <div className="cyber-card-header">
            <Terminal className="w-5 h-5 text-primary" />
            <h3 className="text-lg font-bold uppercase tracking-wider text-foreground">基本信息</h3>
          </div>
          <div className="p-6 space-y-6">
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <div className="space-y-2">
                <Label htmlFor="email" className="text-xs font-bold text-muted-foreground uppercase flex items-center gap-2">
                  <Mail className="w-3 h-3" /> 邮箱
                </Label>
                <Input
                  id="email"
                  value={profile?.email || ""}
                  disabled
                  className="cyber-input bg-muted text-muted-foreground cursor-not-allowed"
                />
                <p className="text-xs text-muted-foreground">邮箱不可修改</p>
              </div>
              <div className="space-y-2">
                <Label htmlFor="full_name" className="text-xs font-bold text-muted-foreground uppercase flex items-center gap-2">
                  <User className="w-3 h-3" /> 姓名
                </Label>
                <Input
                  id="full_name"
                  value={form.full_name}
                  onChange={(e) => setForm({ ...form, full_name: e.target.value })}
                  placeholder="请输入姓名"
                  className="cyber-input"
                />
              </div>
              <div className="space-y-2">
                <Label htmlFor="phone" className="text-xs font-bold text-muted-foreground uppercase flex items-center gap-2">
                  <Phone className="w-3 h-3" /> 手机号
                </Label>
                <Input
                  id="phone"
                  value={form.phone}
                  onChange={(e) => setForm({ ...form, phone: e.target.value })}
                  placeholder="请输入手机号"
                  className="cyber-input"
                />
              </div>
            </div>

            <div className="pt-6 border-t border-border">
              <h3 className="section-title text-sm mb-4 flex items-center gap-2">
                <GitBranch className="w-4 h-4" />
                代码托管账号
              </h3>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                <div className="space-y-2">
                  <Label htmlFor="github" className="text-xs font-bold text-muted-foreground uppercase flex items-center gap-2">
                    <GitBranch className="w-3 h-3" /> GitHub 用户名
                  </Label>
                  <Input
                    id="github"
                    value={form.github_username}
                    onChange={(e) => setForm({ ...form, github_username: e.target.value })}
                    placeholder="your-github-username"
                    className="cyber-input"
                  />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="gitlab" className="text-xs font-bold text-muted-foreground uppercase flex items-center gap-2">
                    <GitBranch className="w-3 h-3" /> GitLab 用户名
                  </Label>
                  <Input
                    id="gitlab"
                    value={form.gitlab_username}
                    onChange={(e) => setForm({ ...form, gitlab_username: e.target.value })}
                    placeholder="your-gitlab-username"
                    className="cyber-input"
                  />
                </div>
              </div>
            </div>

            <div className="flex justify-end pt-4">
              <Button onClick={handleSave} disabled={saving} className="cyber-btn-primary h-10">
                {saving ? (
                  <>
                    <div className="loading-spinner w-4 h-4 mr-2" />
                    保存中...
                  </>
                ) : (
                  <>
                    <Save className="w-4 h-4 mr-2" />
                    保存修改
                  </>
                )}
              </Button>
            </div>
          </div>
        </div>

        {/* Password Change */}
        <div className="lg:col-span-3 cyber-card p-0">
          <div className="cyber-card-header">
            <KeyRound className="w-5 h-5 text-amber-400" />
            <h3 className="text-lg font-bold uppercase tracking-wider text-foreground">修改密码</h3>
          </div>
          <div className="p-6">
            <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
              <div className="space-y-2">
                <Label htmlFor="new_password" className="text-xs font-bold text-muted-foreground uppercase">新密码</Label>
                <Input
                  id="new_password"
                  type="password"
                  value={passwordForm.new_password}
                  onChange={(e) => setPasswordForm({ ...passwordForm, new_password: e.target.value })}
                  placeholder="输入新密码"
                  className="cyber-input"
                />
              </div>
              <div className="space-y-2">
                <Label htmlFor="confirm_password" className="text-xs font-bold text-muted-foreground uppercase">确认密码</Label>
                <Input
                  id="confirm_password"
                  type="password"
                  value={passwordForm.confirm_password}
                  onChange={(e) => setPasswordForm({ ...passwordForm, confirm_password: e.target.value })}
                  placeholder="再次输入新密码"
                  className="cyber-input"
                />
              </div>
              <div className="flex items-end">
                <Button
                  onClick={handleChangePassword}
                  disabled={changingPassword}
                  className="cyber-btn-outline h-10"
                >
                  {changingPassword ? (
                    <>
                      <div className="loading-spinner w-4 h-4 mr-2" />
                      更新中...
                    </>
                  ) : (
                    <>
                      <KeyRound className="w-4 h-4 mr-2" />
                      更新密码
                    </>
                  )}
                </Button>
              </div>
            </div>
          </div>
        </div>
        {/* API Key Management */}
        <div className="lg:col-span-3 cyber-card p-0">
          <div className="cyber-card-header">
            <Code2 className="w-5 h-5 text-emerald-400" />
            <h3 className="text-lg font-bold uppercase tracking-wider text-foreground">API Key（三方接口调用）</h3>
          </div>
          <div className="p-6 space-y-4">
            {apiKeyInfo?.has_api_key ? (
              <div className="space-y-4">
                <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                  <div className="space-y-1">
                    <p className="text-xs font-bold text-muted-foreground uppercase">Key</p>
                    <p className="text-foreground font-mono text-sm break-all">
                      {apiKeyInfo.api_key_prefix}****
                    </p>
                  </div>
                  <div className="space-y-1">
                    <p className="text-xs font-bold text-muted-foreground uppercase">创建时间</p>
                    <p className="text-foreground font-mono text-sm">{formatDate(apiKeyInfo.api_key_created_at)}</p>
                  </div>
                  <div className="space-y-1">
                    <p className="text-xs font-bold text-muted-foreground uppercase">有效期至</p>
                    <p className={`font-mono text-sm ${apiKeyInfo.is_expired ? 'text-rose-400' : 'text-emerald-400'}`}>
                      {formatDate(apiKeyInfo.api_key_expires_at)}
                      {apiKeyInfo.is_expired ? '（已过期）' : ''}
                    </p>
                  </div>
                </div>
                <div className="flex flex-wrap gap-3 pt-2">
                  <Button
                    variant="outline"
                    onClick={() => setShowKeyPasswordDialog(true)}
                    disabled={apiKeyBusy}
                    className="cyber-btn-outline h-10"
                  >
                    <RefreshCw className="w-4 h-4 mr-2" />
                    重新生成
                  </Button>
                  <Button
                    variant="destructive"
                    onClick={() => setShowRevokeDialog(true)}
                    disabled={apiKeyBusy}
                    className="bg-rose-500/20 hover:bg-rose-500/30 text-rose-400 border border-rose-500/30 h-10"
                  >
                    <Ban className="w-4 h-4 mr-2" />
                    吊销
                  </Button>
                </div>
              </div>
            ) : (
              <div className="flex flex-col md:flex-row md:items-center md:justify-between gap-4">
                <p className="text-muted-foreground text-sm">
                  尚未生成 API Key。生成时需输入登录密码确认，生成后三方系统可通过 <span className="text-primary font-mono">X-API-Key</span> 请求头直接调用审计报告等接口，默认有效期一年。
                </p>
                <Button
                  onClick={() => setShowKeyPasswordDialog(true)}
                  disabled={apiKeyBusy}
                  className="cyber-btn-primary h-10 shrink-0"
                >
                  {apiKeyBusy ? (
                    <>
                      <div className="loading-spinner w-4 h-4 mr-2" />
                      生成中...
                    </>
                  ) : (
                    <>
                      <KeyRound className="w-4 h-4 mr-2" />
                      生成 API Key
                    </>
                  )}
                </Button>
              </div>
            )}

            {/* Usage example */}
            <div className="pt-4 border-t border-border">
              <p className="text-xs font-bold text-muted-foreground uppercase mb-2">调用示例</p>
              <pre className="bg-muted/50 border border-border rounded-md p-3 text-xs text-emerald-400 overflow-x-auto">
{`curl -H "X-API-Key: <你的API Key>" \\
  "http://<后端地址>/api/v1/agent-tasks/{task_id}/report?format=json"`}
              </pre>
            </div>
          </div>
        </div>
      </div>

      {/* New API Key Dialog（仅展示一次） */}
      <AlertDialog open={!!newApiKey} onOpenChange={(open) => !open && setNewApiKey(null)}>
        <AlertDialogContent className="cyber-card cyber-dialog">
          <AlertDialogHeader>
            <AlertDialogTitle className="text-lg font-bold uppercase text-foreground flex items-center gap-2">
              <KeyRound className="w-5 h-5 text-emerald-400" />
              API Key 已生成
            </AlertDialogTitle>
            <AlertDialogDescription asChild>
              <div className="space-y-3 w-full">
                <p className="text-amber-400 text-sm">
                  完整 Key 仅显示这一次，服务端只保存哈希，请立即复制并妥善保存！
                </p>
                <div className="flex items-center gap-2">
                  <code className="flex-1 bg-muted/50 border border-border rounded-md p-3 text-xs text-emerald-400 font-mono break-all">
                    {newApiKey}
                  </code>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => copyText(newApiKey || "")}
                    className="cyber-btn-outline shrink-0"
                  >
                    <Copy className="w-4 h-4 mr-1" />
                    复制
                  </Button>
                </div>
              </div>
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogAction className="cyber-btn-primary" onClick={() => setNewApiKey(null)}>
              我已保存
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Key Password Confirm Dialog（生成/重新生成共用） */}
      <AlertDialog open={showKeyPasswordDialog} onOpenChange={(open) => { setShowKeyPasswordDialog(open); if (!open) setKeyPassword(""); }}>
        <AlertDialogContent className="cyber-card cyber-dialog">
          <AlertDialogHeader>
            <AlertDialogTitle className="text-lg font-bold uppercase text-foreground flex items-center gap-2">
              <KeyRound className="w-5 h-5 text-amber-400" />
              {apiKeyInfo?.has_api_key ? "重新生成 API Key" : "生成 API Key"}
            </AlertDialogTitle>
            <AlertDialogDescription asChild>
              <div className="space-y-3 w-full">
                <p className="text-muted-foreground">
                  为保障账号安全，请输入登录密码进行确认。
                  {apiKeyInfo?.has_api_key ? "重新生成后旧 Key 立即失效，请及时通知三方更新。" : ""}
                </p>
                <Input
                  type="password"
                  value={keyPassword}
                  onChange={(e) => setKeyPassword(e.target.value)}
                  placeholder="登录密码"
                  className="cyber-input"
                  autoFocus
                  onKeyDown={(e) => e.key === "Enter" && keyPassword && handleGenerateApiKey()}
                />
              </div>
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel className="cyber-btn-outline">取消</AlertDialogCancel>
            <AlertDialogAction
              onClick={(e) => { e.preventDefault(); handleGenerateApiKey(); }}
              disabled={!keyPassword || apiKeyBusy}
              className="cyber-btn-primary"
            >
              {apiKeyBusy ? (
                <>
                  <div className="loading-spinner w-4 h-4 mr-2" />
                  处理中...
                </>
              ) : (
                "确认并生成"
              )}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Revoke Confirm Dialog */}
      <AlertDialog open={showRevokeDialog} onOpenChange={setShowRevokeDialog}>
        <AlertDialogContent className="cyber-card border-rose-500/30 cyber-dialog">
          <AlertDialogHeader>
            <AlertDialogTitle className="text-lg font-bold uppercase text-foreground flex items-center gap-2">
              <Ban className="w-5 h-5 text-rose-400" />
              确认吊销 API Key？
            </AlertDialogTitle>
            <AlertDialogDescription className="text-muted-foreground">
              吊销后该 Key 立即失效，三方将无法再通过它调用接口。此操作不可撤销，可随时重新生成新 Key。
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel className="cyber-btn-outline">取消</AlertDialogCancel>
            <AlertDialogAction
              onClick={handleRevokeApiKey}
              className="bg-rose-500/20 hover:bg-rose-500/30 text-rose-400 border border-rose-500/30"
            >
              确认吊销
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Logout Confirmation Dialog */}
      <AlertDialog open={showLogoutDialog} onOpenChange={setShowLogoutDialog}>
        <AlertDialogContent className="cyber-card border-rose-500/30 cyber-dialog">
          <AlertDialogHeader>
            <AlertDialogTitle className="text-lg font-bold uppercase text-foreground flex items-center gap-2">
              <LogOut className="w-5 h-5 text-rose-400" />
              确认退出登录？
            </AlertDialogTitle>
            <AlertDialogDescription className="text-muted-foreground">
              退出后需要重新登录才能访问系统。
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel className="cyber-btn-outline">
              取消
            </AlertDialogCancel>
            <AlertDialogAction
              onClick={handleLogout}
              className="bg-rose-500/20 hover:bg-rose-500/30 text-rose-400 border border-rose-500/30"
            >
              确认退出
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
