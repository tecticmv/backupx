import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import { installApiFetch } from './lib/api'

// Adds the CSRF header to every mutating API request
installApiFetch()

const rootElement = document.getElementById('root') || document.getElementById('settings-root')

if (rootElement) {
  createRoot(rootElement).render(
    <StrictMode>
      <App />
    </StrictMode>,
  )
}
