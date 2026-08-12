import { useEffect, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { toast } from 'sonner';
import { useAuth } from '@/lib/auth-context';
import { api } from '@/lib/api-client';
import { Button } from '@/components/primitives/button';
import { Input } from '@/components/primitives/input';
import { useLang, LanguageToggle } from '@/i18n';

export function SignupPage() {
  const { signup } = useAuth();
  const navigate = useNavigate();
  const { lang, t } = useLang();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [accepted, setAccepted] = useState(false);
  const [pending, setPending] = useState(false);
  // TOS texts are fetched from the server so the dashboard and the
  // hash audit log stay in lockstep without manual duplication. We
  // keep both languages in memory so toggling is instant.
  const [tosTexts, setTosTexts] = useState<{ en: string; es: string } | null>(
    null
  );
  const [tosLoadError, setTosLoadError] = useState(false);

  useEffect(() => {
    let cancelled = false;
    api
      .getTos()
      .then((r) => {
        if (cancelled) return;
        if (r.texts) {
          setTosTexts(r.texts);
        } else {
          setTosTexts({ en: r.text, es: r.text });
        }
      })
      .catch(() => {
        if (!cancelled) setTosLoadError(true);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const passwordError =
    confirm && password !== confirm
      ? t('auth.signup.passwordsDontMatch')
      : undefined;
  const canSubmit =
    !!email &&
    password.length >= 8 &&
    password === confirm &&
    accepted &&
    !!tosTexts &&
    !pending;

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!canSubmit) return;
    setPending(true);
    try {
      await signup(email, password, lang);
      toast.success(t('auth.signup.accountCreated'));
      navigate('/onboarding/grvt', { replace: true });
    } catch (err) {
      toast.error((err as Error).message || t('auth.signup.signupFailed'));
    } finally {
      setPending(false);
    }
  }

  const termsBody = tosTexts ? tosTexts[lang] : '';

  return (
    <div className="min-h-dvh flex items-center justify-center p-4 bg-bg-base">
      <div className="w-full max-w-md space-y-6">
        <div className="flex justify-end">
          <LanguageToggle />
        </div>
        <div className="text-center">
          <h1 className="text-2xl font-bold tracking-tight text-text-primary">
            {t('auth.signup.title')}
          </h1>
          <p className="text-sm text-text-muted mt-1">
            {t('auth.signup.subtitle')}
          </p>
        </div>

        <div className="rounded-md border border-primary/40 bg-primary/5 p-4 space-y-2">
          <div className="text-xs font-medium text-text-primary">
            {t('auth.signup.grvtReferralTitle')}
          </div>
          <p className="text-xs text-text-secondary leading-relaxed">
            {t('auth.signup.grvtReferralBody')}
          </p>
          <a
            href="https://grvt.io/?ref=R3WLGZS"
            target="_blank"
            rel="noopener noreferrer"
            className="inline-block text-xs font-medium text-primary hover:underline"
          >
            {t('auth.signup.grvtReferralLink')}
          </a>
          <p className="text-2xs text-text-muted pt-1 border-t border-border-subtle">
            {t('auth.signup.grvtReferralAfter')}
          </p>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4">
          <Input
            label={t('auth.signup.email')}
            type="email"
            autoComplete="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            disabled={pending}
          />
          <Input
            label={t('auth.signup.password')}
            type="password"
            autoComplete="new-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            disabled={pending}
          />
          <Input
            label={t('auth.signup.confirmPassword')}
            type="password"
            autoComplete="new-password"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            error={passwordError}
            disabled={pending}
          />

          <div className="rounded-md border border-border-subtle bg-bg-surface p-3 space-y-2">
            <div className="text-2xs uppercase tracking-wider text-text-muted">
              {t('auth.signup.termsTitle')}
            </div>
            {tosLoadError ? (
              <div className="text-xs text-danger">
                {t('common.networkError')}
              </div>
            ) : !tosTexts ? (
              <div className="text-xs text-text-muted animate-pulse">
                {t('common.loading')}
              </div>
            ) : (
              <pre className="text-2xs text-text-secondary whitespace-pre-wrap font-sans leading-relaxed max-h-40 overflow-y-auto">
                {termsBody}
              </pre>
            )}
            <label className="flex items-start gap-2 text-xs text-text-secondary cursor-pointer pt-1 border-t border-border-subtle">
              <input
                type="checkbox"
                checked={accepted}
                onChange={(e) => setAccepted(e.target.checked)}
                className="mt-0.5 size-4 accent-primary"
                disabled={pending || !tosTexts}
              />
              <span>{t('auth.signup.acceptTerms')}</span>
            </label>
          </div>

          <Button
            variant="primary"
            type="submit"
            disabled={!canSubmit}
            className="w-full"
          >
            {pending ? t('auth.signup.creating') : t('auth.signup.createBtn')}
          </Button>
        </form>

        <p className="text-xs text-text-muted text-center">
          {t('auth.signup.haveAccount')}{' '}
          <Link to="/login" className="text-primary hover:underline">
            {t('auth.signup.signIn')}
          </Link>
        </p>
      </div>
    </div>
  );
}
