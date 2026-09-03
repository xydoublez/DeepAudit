import { createContext, useContext, useState, useEffect, ReactNode } from 'react';
import { apiClient } from '../api/serverClient';

interface User {
  id: string;
  email: string;
  full_name: string;
  role: string;
  avatar_url?: string;
}

interface AuthContextType {
  user: User | null;
  isAuthenticated: boolean;
  isLoading: boolean;
  login: (token: string, rememberMe?: boolean) => Promise<void>;
  logout: (redirectSso?: boolean) => void;
}

const AuthContext = createContext<AuthContextType | undefined>(undefined);

export const AuthProvider = ({ children }: { children: ReactNode }) => {
  const [user, setUser] = useState<User | null>(null);
  const [isAuthenticated, setIsAuthenticated] = useState(false);
  const [isLoading, setIsLoading] = useState(true);

  useEffect(() => {
    const checkAuth = async () => {
      // Check both localStorage (remember me) and sessionStorage (session only)
      const token = localStorage.getItem('access_token') || sessionStorage.getItem('access_token');
      if (token) {
        try {
          const response = await apiClient.get('/users/me');
          setUser(response.data);
          setIsAuthenticated(true);
        } catch (error) {
          console.error('Auth check failed', error);
          // 被动失效：仅本地清理，不触发 Casdoor 单点登出（避免误登出 IdP）
          logout();
        }
      }
      setIsLoading(false);
    };

    checkAuth();
  }, []);

  const login = async (token: string, rememberMe: boolean = false) => {
    // Clear any existing tokens first
    localStorage.removeItem('access_token');
    sessionStorage.removeItem('access_token');

    // Store token based on rememberMe preference
    if (rememberMe) {
      localStorage.setItem('access_token', token);
    } else {
      sessionStorage.setItem('access_token', token);
    }

    try {
        const response = await apiClient.get('/users/me');
        setUser(response.data);
        setIsAuthenticated(true);
    } catch (e) {
        console.error("Login fetch user failed", e);
    }
  };

  const logout = (redirectSso: boolean = false) => {
    const provider =
      sessionStorage.getItem('auth_provider') || localStorage.getItem('auth_provider');
    localStorage.removeItem('access_token');
    sessionStorage.removeItem('access_token');
    localStorage.removeItem('auth_provider');
    sessionStorage.removeItem('auth_provider');
    setUser(null);
    setIsAuthenticated(false);

    // SSO 会话且显式请求时，跳转后端 /auth/sso/logout 实现单点登出
    // （后端会重定向到 Casdoor CAS 登出页并回跳前端登录页）
    if (redirectSso && provider === 'sso') {
      const apiBase = import.meta.env.VITE_API_BASE_URL || '/api/v1';
      window.location.href = `${apiBase}/auth/sso/logout`;
    }
  };

  return (
    <AuthContext.Provider value={{ user, isAuthenticated, isLoading, login, logout }}>
      {children}
    </AuthContext.Provider>
  );
};

export const useAuth = () => {
  const context = useContext(AuthContext);
  if (context === undefined) {
    throw new Error('useAuth must be used within an AuthProvider');
  }
  return context;
};
