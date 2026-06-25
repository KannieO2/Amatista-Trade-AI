import { useState } from 'react';
import { Link } from 'react-router-dom';
import { toast } from 'sonner';
import { api } from '@/lib/api-client';
import { Button } from '@/components/primitives/button';
import { Input } from '@/components/primitives/input';
import { LanguageToggle, useT } from '@/i18n';

export function ForgotPasswordPage() {
  const t = useT();
  const [email, setEmail] = useState('');
  const [pending, setPending] = useState(false);
  const [submitted, setSubmitted] = useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!email) return;
    setPending(true);
    try {
      await api.forgotPassword(email);
      setSubmitted(true);
    } catch (err) {
      toast.error((err as Error).message || t('auth.forgotPassword.failed'));
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
            {t('auth.forgotPassword.title')}
          </h1>
          <p className="text-sm text-text-muted mt-1">
            {t('auth.forgotPassword.subtitle')}
          </p>
        </div>

        {submitted ? (
          <div className="rounded-md border border-border-default bg-bg-elevated p-4 text-sm text-text-muted space-y-3">
            <p className="text-text-primary font-medium">
              {t('auth.forgotPassword.sentTitle')}
            </p>
            <p>{t('auth.forgotPassword.sentBody')}</p>
            <p className="text-xs">{t('auth.forgotPassword.smtpDisabled')}</p>
          </div>
        ) : (
          <form onSubmit={handleSubmit} className="space-y-4">
            <Input
              label={t('auth.forgotPassword.email')}
              type="email"
              autoComplete="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              disabled={pending}
            />
            <Button
              variant="primary"
              type="submit"
              disabled={pending || !email}
              className="w-full"
            >
              {pending
                ? t('auth.forgotPassword.sending')
                : t('auth.forgotPassword.sendBtn')}
            </Button>
          </form>
        )}

        <p className="text-xs text-text-muted text-center">
          <Link to="/login" className="text-primary hover:underline">
            {t('auth.forgotPassword.backToLogin')}
          </Link>
        </p>
      </div>
    </div>
  );
}
