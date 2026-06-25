import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { toast } from 'sonner';
import { api } from '@/lib/api-client';
import { useAuth } from '@/lib/auth-context';
import { Button } from '@/components/primitives/button';
import { Input } from '@/components/primitives/input';
import { Card } from '@/components/primitives/card';
import { LanguageToggle, useT } from '@/i18n';

export function GrvtOnboardingPage() {
  const t = useT();
  const { refreshMe } = useAuth();
  const navigate = useNavigate();
  const [apiKey, setApiKey] = useState('');
  const [apiSecret, setApiSecret] = useState('');
  const [tradingAddress, setTradingAddress] = useState('');
  const [accountId, setAccountId] = useState('');
  const [subAccountId, setSubAccountId] = useState('');
  const [pending, setPending] = useState(false);

  // Sub-account is now optional: the backend defaults to accountId
  // when empty, covering the 90% of GRVT users with a single
  // sub-account where both ids match.
  const canSave =
    apiKey.length > 0 &&
    /^0x[0-9a-fA-F]{64}$/.test(apiSecret) &&
    /^0x[0-9a-fA-F]{40}$/.test(tradingAddress) &&
    accountId.length > 0 &&
    !pending;

  async function handleSave(e: React.FormEvent) {
    e.preventDefault();
    if (!canSave) return;
    setPending(true);
    try {
      await api.saveGrvtCredentials({
        apiKey,
        apiSecret,
        tradingAddress,
        accountId,
        subAccountId: subAccountId.trim() || undefined,
      });
      toast.success(t('onboarding.grvt.saved'));
      await refreshMe();
      navigate('/', { replace: true });
    } catch (err) {
      toast.error((err as Error).message || t('onboarding.grvt.saveFailed'));
    } finally {
      setPending(false);
    }
  }

  return (
    <div className="min-h-dvh flex items-center justify-center p-4 bg-bg-base">
      <div className="w-full max-w-lg space-y-6">
        <div className="flex justify-end">
          <LanguageToggle />
        </div>
        <div className="text-center">
          <h1 className="text-2xl font-bold tracking-tight text-text-primary">
            {t('onboarding.grvt.title')}
          </h1>
          <p className="text-sm text-text-muted mt-1">
            {t('onboarding.grvt.subtitle')}
          </p>
        </div>

        <Card>
          <div className="text-2xs text-text-muted space-y-1 mb-4">
            <p>
              {t('onboarding.grvt.instructionsPrefix')}
              <a
                href="https://grvt.io"
                target="_blank"
                rel="noopener noreferrer"
                className="text-primary hover:underline"
              >
                grvt.io
              </a>
              {t('onboarding.grvt.instructionsSuffix')}
            </p>
            <p className="text-warning">
              {t('onboarding.grvt.encryptionNote')}
            </p>
          </div>

          <form onSubmit={handleSave} className="space-y-3">
            <Input
              label={t('onboarding.grvt.apiKey')}
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              disabled={pending}
              autoComplete="off"
            />
            <Input
              label={t('onboarding.grvt.apiSecret')}
              type="password"
              value={apiSecret}
              onChange={(e) => setApiSecret(e.target.value)}
              disabled={pending}
              autoComplete="off"
              error={
                apiSecret && !/^0x[0-9a-fA-F]{64}$/.test(apiSecret)
                  ? t('onboarding.grvt.apiSecretError')
                  : undefined
              }
            />
            <Input
              label={t('onboarding.grvt.tradingAddress')}
              value={tradingAddress}
              onChange={(e) => setTradingAddress(e.target.value)}
              disabled={pending}
              autoComplete="off"
              error={
                tradingAddress && !/^0x[0-9a-fA-F]{40}$/.test(tradingAddress)
                  ? t('onboarding.grvt.tradingAddressError')
                  : undefined
              }
            />
            <Input
              label={t('onboarding.grvt.accountId')}
              value={accountId}
              onChange={(e) => setAccountId(e.target.value)}
              disabled={pending}
              autoComplete="off"
            />
            <div className="space-y-1">
              <Input
                label={t('onboarding.grvt.subAccountId')}
                value={subAccountId}
                onChange={(e) => setSubAccountId(e.target.value)}
                disabled={pending}
                autoComplete="off"
              />
              <p className="text-2xs text-text-muted px-1">
                {t('onboarding.grvt.subAccountIdHint')}
              </p>
            </div>

            <Button
              variant="primary"
              type="submit"
              disabled={!canSave}
              className="w-full"
            >
              {pending ? t('onboarding.grvt.saving') : t('onboarding.grvt.saveBtn')}
            </Button>
          </form>
        </Card>

        <p className="text-2xs text-text-muted text-center">
          {t('onboarding.grvt.canUpdateLater')}
        </p>
      </div>
    </div>
  );
}
