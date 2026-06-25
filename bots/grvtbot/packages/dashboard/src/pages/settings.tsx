import { useNavigate } from 'react-router-dom';
import { useAuth } from '@/lib/auth-context';
import { Card } from '@/components/primitives/card';
import { Button } from '@/components/primitives/button';
import { Mono } from '@/components/primitives/mono';
import { SubAccountsCard } from '@/components/sub-accounts-card';
import { useT } from '@/i18n';

export function SettingsPage() {
  const t = useT();
  const { user, logout } = useAuth();
  const navigate = useNavigate();

  return (
    <div className="flex flex-col gap-4">
      <h1 className="text-2xl font-semibold tracking-tight">
        {t('settings.title')}
      </h1>

      <Card>
        <h2 className="text-sm font-semibold mb-3">
          {t('settings.sectionAccount')}
        </h2>
        <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-2 text-xs">
          <dt className="text-text-muted uppercase tracking-wider text-2xs">
            {t('settings.account.email')}
          </dt>
          <dd className="font-mono text-text-secondary">{user?.email}</dd>
          <dt className="text-text-muted uppercase tracking-wider text-2xs">
            {t('settings.account.role')}
          </dt>
          <dd className="text-text-secondary">
            {user?.isAdmin
              ? t('settings.account.admin')
              : t('settings.account.user')}
          </dd>
        </dl>
        <div className="mt-4">
          <Button
            variant="secondary"
            onClick={() => {
              logout();
              navigate('/login', { replace: true });
            }}
          >
            {t('settings.account.logoutBtn')}
          </Button>
        </div>
      </Card>

      <Card>
        <h2 className="text-sm font-semibold mb-3">
          {t('settings.sectionGrvt')}
        </h2>
        {user?.hasGrvtCreds ? (
          <div className="space-y-3">
            <div className="flex items-center gap-2 text-xs">
              <span className="size-2 rounded-full bg-success" />
              <span className="text-text-secondary">
                {t('settings.grvtConnected')}
              </span>
            </div>
            <p className="text-2xs text-text-muted">
              {t('settings.grvtEncryptedNote')}
            </p>
            <Button
              variant="secondary"
              onClick={() => navigate('/onboarding/grvt')}
            >
              {t('settings.grvtUpdateBtn')}
            </Button>
          </div>
        ) : (
          <div className="space-y-3">
            <div className="flex items-center gap-2 text-xs">
              <span className="size-2 rounded-full bg-warning" />
              <span className="text-warning">
                {t('settings.grvtNotConnected')}
              </span>
            </div>
            <p className="text-2xs text-text-muted">
              {t('settings.grvtMissingNote')}
            </p>
            <Button
              variant="primary"
              onClick={() => navigate('/onboarding/grvt')}
            >
              {t('settings.grvtConnectBtn')}
            </Button>
          </div>
        )}
      </Card>

      {user?.hasGrvtCreds && <SubAccountsCard />}

      <Card>
        <h2 className="text-sm font-semibold mb-2">
          {t('settings.sectionConnection')}
        </h2>
        <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-2 text-xs">
          <dt className="text-text-muted uppercase tracking-wider text-2xs">
            {t('settings.apiBase')}
          </dt>
          <dd className="font-mono text-text-secondary">
            {import.meta.env.VITE_API_BASE_URL || t('settings.sameOrigin')}
          </dd>
          <dt className="text-text-muted uppercase tracking-wider text-2xs">
            {t('settings.auth')}
          </dt>
          <dd className="font-mono text-text-secondary">
            JWT (<Mono>userId={user?.id}</Mono>)
          </dd>
        </dl>
      </Card>

      <Card>
        <h2 className="text-sm font-semibold mb-2">
          {t('settings.sectionReferral')}
        </h2>
        <p className="text-2xs text-text-muted">
          {t('settings.referralBody')}
        </p>
        <a
          href="https://grvt.io/?ref=R3WLGZS"
          target="_blank"
          rel="noopener noreferrer"
          className="inline-block mt-2 text-sm text-primary hover:underline"
        >
          {t('settings.referralCta')}
        </a>
      </Card>
    </div>
  );
}
