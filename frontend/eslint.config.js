import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import tseslint from 'typescript-eslint'
import { defineConfig, globalIgnores } from 'eslint/config'

export default defineConfig([
  globalIgnores(['dist']),
  {
    files: ['**/*.{ts,tsx}'],
    extends: [
      js.configs.recommended,
      tseslint.configs.recommended,
      reactHooks.configs.flat.recommended,
      reactRefresh.configs.vite,
    ],
    languageOptions: {
      ecmaVersion: 2020,
      globals: globals.browser,
    },
    rules: {
      // Allow shadcn/ui component patterns
      'react-refresh/only-export-components': ['warn', { allowExportNames: ['badgeVariants', 'buttonVariants', 'useFormField', 'Form', 'FormItem', 'FormLabel', 'FormControl', 'FormDescription', 'FormMessage', 'FormField', 'useSidebar', 'SidebarProvider', 'Sidebar', 'SidebarContent', 'SidebarFooter', 'SidebarGroup', 'SidebarGroupContent', 'SidebarGroupLabel', 'SidebarHeader', 'SidebarInput', 'SidebarInset', 'SidebarMenu', 'SidebarMenuAction', 'SidebarMenuBadge', 'SidebarMenuButton', 'SidebarMenuItem', 'SidebarMenuSkeleton', 'SidebarMenuSub', 'SidebarMenuSubButton', 'SidebarMenuSubItem', 'SidebarRail', 'SidebarSeparator', 'SidebarTrigger', 'useTheme', 'ThemeProvider'] }],
    },
  },
  // Ignore purity warnings in UI components that use patterns like Math.random for skeleton loading
  {
    files: ['src/components/ui/**/*.{ts,tsx}'],
    rules: {
      'react-hooks/purity': 'off',
    },
  },
])
