/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

import { useEffect } from 'react';

export default function App() {
  useEffect(() => {
    window.location.replace('/frontend/index.html');
  }, []);

  return (
    <div className="min-h-screen bg-[#07080d] text-white flex items-center justify-center font-sans">
      <div className="text-center space-y-2">
        <h1 className="text-2xl font-semibold tracking-tight">Universe</h1>
        <p className="text-sm text-zinc-400">Initializing Sovereign Constellation…</p>
      </div>
    </div>
  );
}
