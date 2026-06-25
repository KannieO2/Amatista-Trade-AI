import { useState } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import { toast } from 'sonner';
import { api } from '@/lib/api-client';
import { Button } from '@/components/primitives/button';
import { Input } from '@/components/primitives/input';
import { LanguageToggle, useT } from '@/i18n';

export function ResetPasswordPage() {
  const t = useT();
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const token = params.get('token') ?? '';
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [pending, setPending] = useState(false);

  const tooShort = password.length > 0 && password.length < 8;
  const mismatch = confirm.length > 0 && password !== confirm;
  const canSubmit =
    !!token && password.length >= 8 && password === confirm && !pending;

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!canSubmit) return;
    setPending(true);
    try {
      await api.resetPassword(token, password);
      toast.success(t('auth.resetPassword.successBody'));
      navigate('/login', { replace: true });
    } catch (err) {
      toast.error((err as Error).message || t('auth.resetPassword.failed'));
    } finally {
      setPending(false);
    }
  }

  if (!token) {
    return (
      <div className="min-h-dvh flex items-center justify-center p-4 bg-bg-base">
        <div className="w-full max-w-sm space-y-4 text-center">
          <div className="flex justify-end">
            <LanguageToggle />
          </div>
          <h1 className="text-2xl font-bold text-text-primary">
            {t('auth.resetPassword.invalidToken')}
          </h1>
          <p className="text-sm text-text-muted">
            {t('auth.resetPassword.missingToken')}
          </p>
          <Link to="/forgot-password" className="text-primary hover:underline text-sm">
            {t('auth.forgotPassword.sendBtn')}
          </Link>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-dvh flex items-center justify-center p-4 bg-bg-base">
      <div className="w-full max-w-sm space-y-6">
        <div className="flex justify-end">
          <LanguageToggle />
        </div>
        <div className="text-center">
          <h1 className="text-2xl font-bold tracking-tight text-text-primary">
            {t('auth.resetPassword.title')}
          </h1>
          <p className="text-sm text-text-muted mt-1">
            {t('auth.resetPassword.subtitle')}
          </p>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4">
          <Input
            label={t('auth.resetPassword.newPassword')}
            type="password"
            autoComplete="new-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            disabled={pending}
            error={tooShort ? t('auth.signup.password') : undefined}
          />
          <Input
            label={t('auth.resetPassword.confirmPassword')}
            type="password"
            autoComplete="new-password"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            disabled={pending}
            error={
              mismatch ? t('auth.resetPassword.passwordsDontMatch') : undefined
            }
          />
          <Button
            variant="primary"
            type="submit"
            disabled={!canSubmit}
            className="w-full"
          >
            {pending
              ? t('auth.resetPassword.resetting')
              : t('auth.resetPassword.resetBtn')}
          </Button>
        </form>

        <p className="text-xs text-text-muted text-center">
          <Link to="/login" className="text-primary hover:underline">
            {t('auth.resetPassword.backToLogin')}
          </Link>
        </p>
      </div>
    </div>
  );
}
