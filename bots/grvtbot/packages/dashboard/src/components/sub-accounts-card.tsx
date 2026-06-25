// H.5: GRVT sub-accounts management card. Renders inside Settings.
// Each row = one entry in `grvt_sub_accounts` (NOT the default
// credentials, which live in `grvt_credentials` and have their own
// onboarding flow). Power users add extras here so different bots
// can route through isolated sub-accounts.

import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import { Plus, Trash2, Star } from 'lucide-react';
import { api } from '@/lib/api-client';
import { Card } from './primitives/card';
import { Button } from './primitives/button';
import { Input } from './primitives/input';
import { Modal } from './primitives/modal';
import { useConfirm } from './primitives/confirm-dialog';
import { useT } from '@/i18n';

interface AddState {
  label: string;
  apiKey: string;
  apiSecret: string;
  tradingAddress: string;
  accountId: string;
  subAccountId: string;
  isDefault: boolean;
}

const INITIAL_ADD: AddState = {
  label: '',
  apiKey: '',
  apiSecret: '',
  tradingAddress: '',
  accountId: '',
  subAccountId: '',
  isDefault: false,
};

export function SubAccountsCard() {
  const t = useT();
  const queryClient = useQueryClient();
  const confirm = useConfirm();
  const [addOpen, setAddOpen] = useState(false);
  const [addState, setAddState] = useState<AddState>(INITIAL_ADD);

  const listQuery = useQuery({
    queryKey: ['sub-accounts'],
    queryFn: () => api.listSubAccounts(),
  });

  const createMutation = useMutation({
    mutationFn: (body: AddState) =>
      api.createSubAccount({
        ...body,
        subAccountId: body.subAccountId.trim() || undefined,
      }),
    onSuccess: () => {
      toast.success(t('settings.subAccounts.added'));
      setAddOpen(false);
      setAddState(INITIAL_ADD);
      queryClient.invalidateQueries({ queryKey: ['sub-accounts'] });
    },
    onError: (err: Error) => {
      toast.error(err.message || t('settings.subAccounts.addFailed'));
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (id: number) => api.deleteSubAccount(id),
    onSuccess: () => {
      toast.success(t('settings.subAccounts.removed'));
      queryClient.invalidateQueries({ queryKey: ['sub-accounts'] });
    },
    onError: (err: Error) =>
      toast.error(err.message || t('settings.subAccounts.removeFailed')),
  });

  const setDefaultMutation = useMutation({
    mutationFn: (id: number) =>
      api.updateSubAccount(id, { isDefault: true }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['sub-accounts'] });
    },
    onError: (err: Error) => toast.error(err.message),
  });

  // Sub-account is optional here too (backend falls back to accountId).
  const canSubmit =
    addState.label.length > 0 &&
    addState.apiKey.length > 0 &&
    /^0x[0-9a-fA-F]{64}$/.test(addState.apiSecret) &&
    /^0x[0-9a-fA-F]{40}$/.test(addState.tradingAddress) &&
    addState.accountId.length > 0 &&
    !createMutation.isPending;

  async function handleDelete(id: number, label: string) {
    const ok = await confirm({
      variant: 'destructive',
      title: t('settings.subAccounts.confirmRemoveTitle', { label }),
      body: t('settings.subAccounts.confirmRemoveBody'),
      confirmLabel: t('settings.subAccounts.confirmRemoveBtn'),
    });
    if (!ok) return;
    deleteMutation.mutate(id);
  }

  const subs = listQuery.data ?? [];

  return (
    <>
      <Card>
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-sm font-semibold">
            {t('settings.subAccounts.title')}
          </h2>
          <Button
            variant="secondary"
            onClick={() => setAddOpen(true)}
            className="text-xs"
          >
            <Plus className="size-3.5 mr-1 inline" />
            {t('settings.subAccounts.addBtn')}
          </Button>
        </div>
        <p className="text-2xs text-text-muted mb-3">
          {t('settings.subAccounts.subtitle')}
        </p>

        {listQuery.isLoading ? (
          <p className="text-2xs text-text-muted">
            {t('settings.subAccounts.loading')}
          </p>
        ) : subs.length === 0 ? (
          <p className="text-2xs text-text-muted italic">
            {t('settings.subAccounts.empty')}
          </p>
        ) : (
          <ul className="divide-y divide-border-subtle">
            {subs.map((s) => (
              <li
                key={s.id}
                className="flex items-center justify-between py-2 text-xs"
              >
                <div className="flex items-center gap-2">
                  <span className="font-medium text-text-primary">
                    {s.label}
                  </span>
                  {s.isDefault && (
                    <span className="text-2xs text-primary uppercase tracking-wider">
                      {t('settings.subAccounts.default')}
                    </span>
                  )}
                  {s.lastTestOk === false && (
                    <span className="text-2xs text-warning">
                      {t('settings.subAccounts.lastTestFailed')}
                    </span>
                  )}
                </div>
                <div className="flex items-center gap-1">
                  {!s.isDefault && (
                    <button
                      type="button"
                      onClick={() => setDefaultMutation.mutate(s.id)}
                      disabled={setDefaultMutation.isPending}
                      className="p-1 text-text-muted hover:text-primary transition-colors"
                      aria-label={t('settings.subAccounts.starHint')}
                      title={t('settings.subAccounts.starHint')}
                    >
                      <Star className="size-3.5" />
                    </button>
                  )}
                  <button
                    type="button"
                    onClick={() => handleDelete(s.id, s.label)}
                    disabled={deleteMutation.isPending}
                    className="p-1 text-text-muted hover:text-danger transition-colors"
                    aria-label={t('settings.subAccounts.removeHint')}
                    title={t('settings.subAccounts.removeHint')}
                  >
                    <Trash2 className="size-3.5" />
                  </button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </Card>

      <Modal
        open={addOpen}
        onClose={() => {
          if (!createMutation.isPending) {
            setAddOpen(false);
            setAddState(INITIAL_ADD);
          }
        }}
        title={t('settings.subAccounts.modalTitle')}
        description={t('settings.subAccounts.modalDescription')}
        footer={
          <>
            <Button
              variant="ghost"
              onClick={() => {
                setAddOpen(false);
                setAddState(INITIAL_ADD);
              }}
              disabled={createMutation.isPending}
            >
              {t('common.cancel')}
            </Button>
            <Button
              variant="primary"
              disabled={!canSubmit}
              onClick={() => createMutation.mutate(addState)}
            >
              {createMutation.isPending
                ? t('settings.subAccounts.modalTesting')
                : t('settings.subAccounts.modalConfirm')}
            </Button>
          </>
        }
      >
        <div className="space-y-3">
          <Input
            label={t('settings.subAccounts.label')}
            placeholder={t('settings.subAccounts.labelPlaceholder')}
            value={addState.label}
            onChange={(e) =>
              setAddState({ ...addState, label: e.target.value })
            }
            disabled={createMutation.isPending}
            autoComplete="off"
          />
          <Input
            label={t('settings.subAccounts.apiKey')}
            value={addState.apiKey}
            onChange={(e) =>
              setAddState({ ...addState, apiKey: e.target.value })
            }
            disabled={createMutation.isPending}
            autoComplete="off"
          />
          <Input
            label={t('settings.subAccounts.apiSecret')}
            type="password"
            value={addState.apiSecret}
            onChange={(e) =>
              setAddState({ ...addState, apiSecret: e.target.value })
            }
            disabled={createMutation.isPending}
            autoComplete="off"
            error={
              addState.apiSecret &&
              !/^0x[0-9a-fA-F]{64}$/.test(addState.apiSecret)
                ? t('settings.subAccounts.apiSecretError')
                : undefined
            }
          />
          <Input
            label={t('settings.subAccounts.tradingAddress')}
            value={addState.tradingAddress}
            onChange={(e) =>
              setAddState({ ...addState, tradingAddress: e.target.value })
            }
            disabled={createMutation.isPending}
            autoComplete="off"
            error={
              addState.tradingAddress &&
              !/^0x[0-9a-fA-F]{40}$/.test(addState.tradingAddress)
                ? t('settings.subAccounts.tradingAddressError')
                : undefined
            }
          />
          <Input
            label={t('settings.subAccounts.accountId')}
            value={addState.accountId}
            onChange={(e) =>
              setAddState({ ...addState, accountId: e.target.value })
            }
            disabled={createMutation.isPending}
            autoComplete="off"
          />
          <div className="space-y-1">
            <Input
              label={t('settings.subAccounts.subAccountId')}
              value={addState.subAccountId}
              onChange={(e) =>
                setAddState({ ...addState, subAccountId: e.target.value })
              }
              disabled={createMutation.isPending}
              autoComplete="off"
            />
            <p className="text-2xs text-text-muted px-1">
              {t('settings.subAccounts.subAccountIdHint')}
            </p>
          </div>
          <label className="flex items-center gap-2 text-xs">
            <input
              type="checkbox"
              checked={addState.isDefault}
              onChange={(e) =>
                setAddState({ ...addState, isDefault: e.target.checked })
              }
              disabled={createMutation.isPending}
            />
            <span>{t('settings.subAccounts.setDefault')}</span>
          </label>
        </div>
      </Modal>
    </>
  );
}
