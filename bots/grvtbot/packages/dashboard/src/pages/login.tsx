import { useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { toast } from 'sonner';
import { useAuth } from '@/lib/auth-context';
import { Button } from '@/components/primitives/button';
import { Input } from '@/components/primitives/input';
import { LanguageToggle, useT } from '@/i18n';

export function LoginPage() {
  const { login } = useAuth();
  const navigate = useNavigate();
  const t = useT();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [pending, setPending] = useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!email || !password) return;
    setPending(true);
    try {
      await login(email, password);
      navigate('/', { replace: true });
    } catch (err) {
      toast.error((err as Error).message || t('auth.login.loginFailed'));
    } finally {
      setPending(false);
    }
  }

  return (
    <div className="min-h-dvh flex items-center justify-center p-4 bg-bg-base">
      <div className="w-full max-w-sm space-y-6">
        <div className="flex justify-end">
          <LanguageToggle />
        </div>
        <div className="text-center">
          <h1 className="text-2xl font-bold tracking-tight text-text-primary">
            {t('header.brand')}
          </h1>
          <p className="text-sm text-text-muted mt-1">
            {t('auth.login.subtitle')}
          </p>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4">
          <Input
            label={t('auth.login.email')}
            type="email"
            autoComplete="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            disabled={pending}
          />
          <Input
            label={t('auth.login.password')}
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            disabled={pending}
          />
          <div className="flex justify-end">
            <Link
              to="/forgot-password"
              className="text-xs text-text-muted hover:text-primary hover:underline"
            >
              {t('auth.login.forgotPassword')}
            </Link>
          </div>
          <Button
            variant="primary"
            type="submit"
            disabled={pending || !email || !password}
            className="w-full"
          >
            {pending ? t('auth.login.loggingIn') : t('auth.login.loginBtn')}
          </Button>
        </form>

        <p className="text-xs text-text-muted text-center">
          {t('auth.login.noAccount')}{' '}
          <Link to="/signup" className="text-primary hover:underline">
            {t('auth.login.signUp')}
          </Link>
        </p>
      </div>
    </div>
  );
}
