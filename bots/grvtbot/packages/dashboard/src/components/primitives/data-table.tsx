// Generic dense data table — table-based, no virtualization (≤1000 rows is
// fine without it). Sortable columns, optional pagination via slicing.
//
// Used by Fills/Snapshots tabs in Bot Detail. Numbers right-aligned with mono.

import { ChevronDown, ChevronUp } from 'lucide-react';
import { useMemo, useState, type ReactNode } from 'react';
import { cn } from '@/lib/cn';

export interface Column<T> {
  key: string;
  header: string;
  // Cell renderer. Receives the row, returns content (string | ReactNode).
  render: (row: T) => ReactNode;
  // Sort accessor. If undefined, the column isn't sortable.
  sortValue?: (row: T) => number | string;
  // Alignment hint. Default: 'left' for text, 'right' for numbers.
  align?: 'left' | 'right' | 'center';
  // CSS width hint (e.g. '120px', '15%', '1fr'). Optional.
  width?: string;
  // Whether to render with mono font (numbers).
  mono?: boolean;
}

interface DataTableProps<T> {
  rows: T[];
  columns: Column<T>[];
  emptyMessage?: string;
  // Optional row key extractor (defaults to index).
  rowKey?: (row: T, index: number) => string | number;
  // Page size; if 0 or undefined, no pagination.
  pageSize?: number;
}

type SortState = { key: string; dir: 'asc' | 'desc' } | null;

export function DataTable<T>({
  rows,
  columns,
  emptyMessage = 'No data',
  rowKey,
  pageSize,
}: DataTableProps<T>) {
  const [sort, setSort] = useState<SortState>(null);
  const [page, setPage] = useState(0);

  const sorted = useMemo(() => {
    if (!sort) return rows;
    const col = columns.find((c) => c.key === sort.key);
    if (!col?.sortValue) return rows;
    const sortFn = col.sortValue;
    const out = [...rows];
    out.sort((a, b) => {
      const av = sortFn(a);
      const bv = sortFn(b);
      if (av < bv) return sort.dir === 'asc' ? -1 : 1;
      if (av > bv) return sort.dir === 'asc' ? 1 : -1;
      return 0;
    });
    return out;
  }, [rows, sort, columns]);

  const totalPages = pageSize ? Math.ceil(sorted.length / pageSize) : 1;
  const visible = pageSize
    ? sorted.slice(page * pageSize, page * pageSize + pageSize)
    : sorted;

  function toggleSort(key: string) {
    setSort((current) => {
      if (!current || current.key !== key) return { key, dir: 'desc' };
      if (current.dir === 'desc') return { key, dir: 'asc' };
      return null;
    });
    setPage(0);
  }

  if (rows.length === 0) {
    return (
      <div className="text-center py-10 text-sm text-text-muted">
        {emptyMessage}
      </div>
    );
  }

  return (
    <div className="flex flex-col">
      <div className="overflow-x-auto">
        <table className="w-full border-collapse">
          <thead>
            <tr className="bg-bg-surface border-b border-border-subtle">
              {columns.map((col) => {
                const sortable = !!col.sortValue;
                const isActive = sort?.key === col.key;
                return (
                  <th
                    key={col.key}
                    scope="col"
                    aria-sort={
                      isActive
                        ? sort.dir === 'asc'
                          ? 'ascending'
                          : 'descending'
                        : sortable
                          ? 'none'
                          : undefined
                    }
                    style={col.width ? { width: col.width } : undefined}
                    className={cn(
                      'text-2xs uppercase tracking-wider font-semibold text-text-muted',
                      'px-3 py-2',
                      col.align === 'right' ? 'text-right' : 'text-left',
                      sortable && 'cursor-pointer select-none hover:text-text-secondary'
                    )}
                    onClick={() => sortable && toggleSort(col.key)}
                  >
                    <span className="inline-flex items-center gap-1">
                      {col.header}
                      {sortable && isActive && (
                        sort.dir === 'asc' ? (
                          <ChevronUp className="size-3" />
                        ) : (
                          <ChevronDown className="size-3" />
                        )
                      )}
                    </span>
                  </th>
                );
              })}
            </tr>
          </thead>
          <tbody>
            {visible.map((row, i) => (
              <tr
                key={rowKey ? rowKey(row, i) : i}
                className="border-b border-border-subtle hover:bg-bg-muted transition-colors"
              >
                {columns.map((col) => (
                  <td
                    key={col.key}
                    className={cn(
                      'px-3 py-2 text-xs text-text-secondary',
                      col.align === 'right' && 'text-right',
                      col.align === 'center' && 'text-center',
                      col.mono && 'font-mono tabular-nums'
                    )}
                  >
                    {col.render(row)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {pageSize && totalPages > 1 && (
        <div className="flex items-center justify-between px-3 py-2 border-t border-border-subtle">
          <span className="text-2xs text-text-muted">
            Showing {page * pageSize + 1}–{Math.min((page + 1) * pageSize, sorted.length)} of {sorted.length}
          </span>
          <div className="flex items-center gap-1">
            <button
              type="button"
              disabled={page === 0}
              onClick={() => setPage((p) => Math.max(0, p - 1))}
              className="px-2 py-1 text-xs rounded text-text-secondary hover:bg-bg-muted disabled:opacity-40 disabled:cursor-not-allowed"
            >
              Prev
            </button>
            <span className="text-2xs text-text-muted px-2">
              {page + 1} / {totalPages}
            </span>
            <button
              type="button"
              disabled={page >= totalPages - 1}
              onClick={() => setPage((p) => Math.min(totalPages - 1, p + 1))}
              className="px-2 py-1 text-xs rounded text-text-secondary hover:bg-bg-muted disabled:opacity-40 disabled:cursor-not-allowed"
            >
              Next
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
