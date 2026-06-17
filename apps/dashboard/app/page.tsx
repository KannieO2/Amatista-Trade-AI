const candidates = [
  {
    symbol: "MICRO/USDT",
    exchange: "MEXC",
    pumpScore: 91,
    confidence: 83,
    volume: "16x",
    holders: "81%",
    dna: "89%",
    status: "Waiting confirmation",
  },
];

const riskLimits = [
  ["Daily loss", "$250"],
  ["Drawdown", "5%"],
  ["Position size", "$500"],
  ["Open trades", "3"],
  ["Leverage", "2x"],
];

export default function Home() {
  return (
    <main className="shell">
      <aside className="sidebar">
        <div>
          <p className="eyebrow">TradeOS AI</p>
          <h1>Control Center</h1>
        </div>
        <nav>
          <a className="active">Pump Reader</a>
          <a>GRVTBot Pro</a>
          <a>Risk Engine</a>
          <a>Exchange Hub</a>
          <a>Learning</a>
        </nav>
      </aside>

      <section className="content">
        <header className="topbar">
          <div>
            <p className="eyebrow">Capital protection first</p>
            <h2>Pump Reader MVP</h2>
          </div>
          <div className="status">
            <span />
            Risk Engine online
          </div>
        </header>

        <section className="metrics">
          <article>
            <span>Pump Score</span>
            <strong>91</strong>
          </article>
          <article>
            <span>Confidence</span>
            <strong>83</strong>
          </article>
          <article>
            <span>DNA Match</span>
            <strong>89%</strong>
          </article>
          <article>
            <span>Execution Mode</span>
            <strong>Manual</strong>
          </article>
        </section>

        <section className="grid">
          <div className="panel wide">
            <div className="panelHeader">
              <div>
                <p className="eyebrow">Live candidates</p>
                <h3>Signals requiring human review</h3>
              </div>
              <button>Refresh</button>
            </div>

            <div className="table">
              <div className="row head">
                <span>Token</span>
                <span>Exchange</span>
                <span>Score</span>
                <span>Confidence</span>
                <span>Status</span>
              </div>
              {candidates.map((candidate) => (
                <div className="row" key={candidate.symbol}>
                  <span>{candidate.symbol}</span>
                  <span>{candidate.exchange}</span>
                  <span>{candidate.pumpScore}</span>
                  <span>{candidate.confidence}</span>
                  <span className="badge">{candidate.status}</span>
                </div>
              ))}
            </div>
          </div>

          <div className="panel">
            <p className="eyebrow">Risk limits</p>
            <h3>Immutable safety rails</h3>
            <div className="limits">
              {riskLimits.map(([label, value]) => (
                <div key={label}>
                  <span>{label}</span>
                  <strong>{value}</strong>
                </div>
              ))}
            </div>
          </div>

          <div className="panel">
            <p className="eyebrow">Rule status</p>
            <h3>Execution blocked until approval</h3>
            <p className="body">
              Pump Reader can rank and prepare opportunities, but every trade execution must pass human approval and Risk Engine checks.
            </p>
          </div>
        </section>
      </section>
    </main>
  );
}

