# Frontend Development Instructions

## Tech Stack

- **Framework**: React 18 with TypeScript
- **Build Tool**: Vite
- **Styling**: Tailwind CSS v4
- **UI Components**: shadcn/ui (mandatory for all UI elements)
- **State Management**: React hooks (useState, useEffect)
- **HTTP Client**: Native fetch API

## Project Structure

```
frontend/
├── src/
│   ├── components/
│   │   └── ui/           # shadcn/ui components (auto-generated)
│   ├── pages/            # Page components
│   ├── types/            # TypeScript type definitions
│   ├── lib/
│   │   └── utils.ts      # Utility functions (cn helper)
│   ├── App.tsx           # Main app component
│   ├── main.tsx          # Entry point
│   └── index.css         # Global styles + Tailwind
├── components.json       # shadcn/ui configuration
├── vite.config.ts        # Vite configuration
└── tsconfig.json         # TypeScript configuration
```

## UI Component Guidelines

### Required: Use shadcn/ui for ALL UI Elements

All UI components MUST use shadcn/ui. Do not create custom components or use other UI libraries.

### Available Components

Currently installed shadcn/ui components:

- `Button` - All buttons and clickable actions
- `Card` - Container cards with CardHeader, CardTitle, CardDescription, CardContent
- `Input` - Text inputs
- `Label` - Form labels
- `Table` - Data tables with TableHeader, TableBody, TableRow, TableCell, TableHead
- `Dialog` - Modal dialogs with DialogTrigger, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter
- `Form` - Form handling with validation
- `Alert` - Alert messages with AlertDescription
- `Sonner` - Toast notifications (use `toast` from sonner)

### Adding New Components

To add a new shadcn/ui component:

```bash
cd frontend
npx shadcn@latest add <component-name>
```

Example:
```bash
npx shadcn@latest add select
npx shadcn@latest add checkbox
npx shadcn@latest add dropdown-menu
```

Browse available components at: https://ui.shadcn.com/docs/components

## Styling Guidelines

### Dark Theme

The app uses a dark theme by default. The root element has the `dark` class applied:

```tsx
<div className="dark min-h-screen bg-background">
```

### Using Tailwind Classes

Use Tailwind utility classes for layout and spacing:

```tsx
<div className="container mx-auto py-8 px-4">
  <div className="mb-8">
    <h1 className="text-3xl font-bold">Title</h1>
    <p className="text-muted-foreground mt-2">Description</p>
  </div>
</div>
```

### CSS Variables

shadcn/ui uses CSS variables for theming. Key variables:

- `bg-background` - Main background color
- `bg-card` - Card background
- `text-foreground` - Primary text color
- `text-muted-foreground` - Secondary text color
- `border` - Border color
- `primary` - Primary accent color
- `destructive` - Error/danger color

## Component Patterns

### Page Component Template

```tsx
import { useState, useEffect } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { toast } from "sonner";

export default function MyPage() {
  const [data, setData] = useState([]);
  const [isLoading, setIsLoading] = useState(true);

  useEffect(() => {
    fetchData();
  }, []);

  const fetchData = async () => {
    try {
      const response = await fetch("/api/endpoint");
      const result = await response.json();
      setData(result);
    } catch (error) {
      toast.error("Failed to fetch data");
    } finally {
      setIsLoading(false);
    }
  };

  if (isLoading) {
    return (
      <div className="flex items-center justify-center min-h-screen">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-primary"></div>
      </div>
    );
  }

  return (
    <div className="container mx-auto py-8 px-4">
      <Card>
        <CardHeader>
          <CardTitle>Page Title</CardTitle>
        </CardHeader>
        <CardContent>
          {/* Content here */}
        </CardContent>
      </Card>
    </div>
  );
}
```

### Form with Dialog Pattern

```tsx
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter, DialogTrigger } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

function FormDialog() {
  const [isOpen, setIsOpen] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    // Handle form submission
    setIsOpen(false);
  };

  return (
    <Dialog open={isOpen} onOpenChange={setIsOpen}>
      <DialogTrigger asChild>
        <Button>Open Form</Button>
      </DialogTrigger>
      <DialogContent>
        <form onSubmit={handleSubmit}>
          <DialogHeader>
            <DialogTitle>Form Title</DialogTitle>
          </DialogHeader>
          <div className="grid gap-4 py-4">
            <div className="grid gap-2">
              <Label htmlFor="field">Field Name</Label>
              <Input id="field" name="field" required />
            </div>
          </div>
          <DialogFooter>
            <Button type="submit">Save</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
```

### Table Pattern

```tsx
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Button } from "@/components/ui/button";

function DataTable({ items }) {
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Name</TableHead>
          <TableHead>Status</TableHead>
          <TableHead className="text-right">Actions</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {items.map((item) => (
          <TableRow key={item.id}>
            <TableCell className="font-medium">{item.name}</TableCell>
            <TableCell>{item.status}</TableCell>
            <TableCell className="text-right space-x-2">
              <Button variant="outline" size="sm">Edit</Button>
              <Button variant="destructive" size="sm">Delete</Button>
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}
```

### Toast Notifications

```tsx
import { toast } from "sonner";

// Success toast
toast.success("Operation completed successfully");

// Error toast
toast.error("Something went wrong");

// With description
toast.success("Saved", {
  description: "Your changes have been saved.",
});
```

## API Integration

### Fetch Pattern

```tsx
const fetchData = async () => {
  try {
    const response = await fetch("/api/endpoint");
    if (!response.ok) {
      const error = await response.json();
      throw new Error(error.message || "Request failed");
    }
    return await response.json();
  } catch (error) {
    toast.error(error instanceof Error ? error.message : "An error occurred");
    throw error;
  }
};
```

### POST/PUT Pattern

```tsx
const saveData = async (data: FormData) => {
  const response = await fetch("/api/endpoint", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });

  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.error || "Failed to save");
  }

  return await response.json();
};
```

## Development

### Running the Dev Server

```bash
cd frontend
npm install
npm run dev
```

The dev server runs at http://localhost:5173 and proxies `/api` requests to the Flask backend at http://localhost:8088.

### Building for Production

```bash
npm run build
```

Output is in the `dist/` directory.

### Type Checking

```bash
npm run tsc
```

## Path Aliases

Use `@/` to import from the `src` directory:

```tsx
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import type { MyType } from "@/types/mytype";
```

## Best Practices

1. **Always use shadcn/ui components** - Never create custom buttons, inputs, dialogs, etc.
2. **Use TypeScript types** - Define interfaces for all data structures in `src/types/`
3. **Handle loading states** - Show spinners or skeletons while fetching data
4. **Handle errors gracefully** - Use toast notifications for user feedback
5. **Use semantic HTML** - Proper form elements, labels, and accessibility
6. **Keep components focused** - One responsibility per component
7. **Use the `cn` helper** - For conditional class names: `cn("base-class", condition && "conditional-class")`
