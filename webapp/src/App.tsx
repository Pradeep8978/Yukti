// src/App.tsx
import { BrowserRouter, Routes, Route } from "react-router-dom";
import { Layout } from "./components/Layout";
import { useLive } from "./hooks/useLive";
import { Dashboard } from "./pages/Dashboard";
import { Positions, Trades, Journal, Control } from "./pages/index";

export default function App() {
  const live = useLive();

  return (
    <BrowserRouter>
      <Layout connected={live.connected} halted={live.halted}>
        <Routes>
          <Route path="/"          element={<Dashboard  live={live} />} />
          <Route path="/positions" element={<Positions  live={live} />} />
          <Route path="/trades"    element={<Trades />} />
          <Route path="/journal"   element={<Journal />} />
          <Route path="/control"   element={<Control    live={live} />} />
        </Routes>
      </Layout>
    </BrowserRouter>
  );
}
