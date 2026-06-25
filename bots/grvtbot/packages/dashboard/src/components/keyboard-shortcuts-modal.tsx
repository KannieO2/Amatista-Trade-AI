// E.2 — Keyboard shortcuts modal.
// Opened by pressing `?` anywhere in the app.

import { Modal } from './primitives/modal';
import { useT } from '@/i18n';

const SHORTCUTS: Array<{ keys: string; actionKey: string }> = [
  { keys: 'g → o', actionKey: 'keyboard.goToOverview' },
  { keys: 'g → b', actionKey: 'keyboard.goToBots' },
  { keys: 'g → s', actionKey: 'keyboard.goToSettings' },
  { keys: 'n → b', actionKey: 'keyboard.createBot' },
  { keys: 't', actionKey: 'keyboard.toggleTheme' },
  { keys: '?', actionKey: 'keyboard.showShortcuts' },
  { keys: 'Esc', actionKey: 'keyboard.close' },
];

interface Props {
  open: boolean;
  onClose: () => void;
}

export function KeyboardShortcutsModal({ open, onClose }: Props) {
  const t = useT();
  return (
    <Modal open={open} onClose={onClose} title={t('keyboard.title')}>
      <table className="w-full text-sm">
        <tbody>
          {SHORTCUTS.map(({ keys, actionKey }) => (
            <tr key={keys} className="border-b border-border-subtle last:border-0">
              <td className="py-2.5 pr-4">
                <span className="inline-flex gap-1 items-center">
                  {keys.split(' → ').map((k, i) => (
                    <span key={i} className="inline-flex items-center gap-1">
                      {i > 0 && (
                        <span className="text-text-muted mx-0.5">
                          {t('keyboard.then')}
                        </span>
                      )}
                      <kbd className="px-1.5 py-0.5 rounded bg-bg-muted border border-border-subtle text-2xs font-mono font-semibold text-text-secondary">
                        {k}
                      </kbd>
                    </span>
                  ))}
                </span>
              </td>
              <td className="py-2.5 text-text-muted">{t(actionKey)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </Modal>
  );
}
