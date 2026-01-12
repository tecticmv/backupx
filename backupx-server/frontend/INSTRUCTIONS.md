# Frontend Development Instructions

## Tech Stack

- **Framework**: React 18 with TypeScript
- **Build Tool**: Vite
- **Styling**: Tailwind CSS v4
- **UI Components**: shadcn/ui (mandatory for all UI elements)
- **State Management**: React hooks (useState, useEffect)
- **HTTP Client**: Native fetch API
- **Routing**: React Router v6
- **Icons**: Lucide React

## Project Structure

```
frontend/
├── src/
│   ├── components/
│   │   ├── ui/               # shadcn/ui components (auto-generated)
│   │   ├── layout/           # Layout components (Layout, AppSidebar)
│   │   └── [Feature]Modal.tsx # Feature-specific modal components
│   ├── pages/                # Page components
│   ├── types/                # TypeScript type definitions
│   ├── hooks/                # Custom React hooks
│   ├── lib/
│   │   └── utils.ts          # Utility functions (cn helper)
│   ├── App.tsx               # Main app with routes
│   ├── main.tsx              # Entry point
│   └── index.css             # Global styles + Tailwind
├── components.json           # shadcn/ui configuration
├── vite.config.ts            # Vite configuration
└── tsconfig.json             # TypeScript configuration
```

## Architecture Patterns

### Entity Management Pattern

The application follows a consistent pattern for managing entities (S3 configs, servers, jobs, etc.):

1. **Separate reusable resources from jobs** - Resources like S3 storage configs and servers are managed independently and referenced by jobs via selection
2. **Settings page for infrastructure** - S3 configs, servers, and other reusable resources are managed in Settings
3. **Jobs reference resources** - When creating a job, users select from existing resources (S3 config, server) rather than entering details inline

### Modal-First Pattern for Create/Edit

All create and edit operations use modals instead of separate pages:

```tsx
// State for modal control
const [modalOpen, setModalOpen] = useState(false);
const [editingId, setEditingId] = useState<string | undefined>(undefined);

// Open for new item
const openNewModal = () => {
  setEditingId(undefined);
  setModalOpen(true);
};

// Open for editing
const openEditModal = (id: string) => {
  setEditingId(id);
  setModalOpen(true);
};

// In JSX
<FeatureFormModal
  open={modalOpen}
  onOpenChange={setModalOpen}
  itemId={editingId}
  onSuccess={fetchData}
/>
```

### Modal Component Structure

Modal components follow this structure:

```tsx
interface FeatureFormModalProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  itemId?: string;  // undefined = create, string = edit
  onSuccess: () => void;
}

export default function FeatureFormModal({
  open,
  onOpenChange,
  itemId,
  onSuccess,
}: FeatureFormModalProps) {
  const isEditing = !!itemId;

  // Fetch data when modal opens for editing
  useEffect(() => {
    if (open && isEditing) {
      fetchItem();
    } else if (open) {
      resetForm();
    }
  }, [open, itemId]);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl max-h-[90vh] flex flex-col p-0 overflow-hidden">
        <DialogHeader className="px-6 pt-6 pb-4 shrink-0">
          <DialogTitle>{isEditing ? "Edit Item" : "New Item"}</DialogTitle>
          <DialogDescription>...</DialogDescription>
        </DialogHeader>

        <form onSubmit={handleSubmit} className="flex flex-col min-h-0 flex-1 overflow-hidden">
          <div className="flex-1 overflow-y-auto px-6">
            <div className="space-y-6 py-2">
              {/* Form fields with sections */}
            </div>
          </div>

          <DialogFooter className="px-6 py-4 border-t shrink-0">
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" disabled={isSaving}>
              {isSaving && <Loader2 className="h-4 w-4 mr-2 animate-spin" />}
              <Save className="h-4 w-4 mr-2" />
              {isEditing ? "Update" : "Create"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
```

### Form Sections with Icons

Group related form fields with icon headers:

```tsx
<div className="space-y-4">
  <div className="flex items-center gap-2 text-sm font-medium">
    <Server className="h-4 w-4 text-primary" />
    Section Name
  </div>
  <div className="grid gap-4 md:grid-cols-2">
    {/* Form fields */}
  </div>
</div>

<Separator />

<div className="space-y-4">
  <div className="flex items-center gap-2 text-sm font-medium">
    <CloudCog className="h-4 w-4 text-primary" />
    Another Section
  </div>
  {/* More fields */}
</div>
```

### Resource Selection Pattern

When a form needs to reference another resource (e.g., job selecting S3 config):

```tsx
const [resources, setResources] = useState<Resource[]>([]);
const [selectedResourceId, setSelectedResourceId] = useState<string>("");

// Fetch resources when modal opens
useEffect(() => {
  if (open) {
    fetchResources();
  }
}, [open]);

// In form
<div className="space-y-2">
  <Label>Select Resource</Label>
  <Select value={selectedResourceId} onValueChange={setSelectedResourceId}>
    <SelectTrigger>
      <SelectValue placeholder="Select a resource" />
    </SelectTrigger>
    <SelectContent>
      {resources.length === 0 ? (
        <SelectItem value="_empty" disabled>
          No resources available
        </SelectItem>
      ) : (
        resources.map((resource) => (
          <SelectItem key={resource.id} value={resource.id}>
            {resource.name} ({resource.details})
          </SelectItem>
        ))
      )}
    </SelectContent>
  </Select>
  {resources.length === 0 && (
    <p className="text-xs text-amber-600">
      Please configure resources in Settings first
    </p>
  )}
</div>
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
- `Textarea` - Multi-line text inputs
- `Table` - Data tables with TableHeader, TableBody, TableRow, TableCell, TableHead
- `Dialog` - Modal dialogs with DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter
- `Select` - Dropdown selects with SelectTrigger, SelectValue, SelectContent, SelectItem
- `Switch` - Toggle switches
- `Badge` - Status badges
- `Separator` - Visual dividers
- `Form` - Form handling with validation
- `Alert` - Alert messages with AlertDescription
- `Sonner` - Toast notifications (use `toast` from sonner)
- `Sidebar` - App navigation sidebar
- `Breadcrumb` - Page breadcrumbs

### Adding New Components

```bash
cd frontend
npx shadcn@latest add <component-name>
```

Browse available components at: https://ui.shadcn.com/docs/components

## Page Structure Patterns

### Stats Cards at Top

Pages showing data typically have stats cards at the top:

```tsx
<div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
  <Card>
    <CardHeader className="flex flex-row items-center justify-between pb-2">
      <CardTitle className="text-sm font-medium">Total Items</CardTitle>
      <FolderIcon className="h-4 w-4 text-muted-foreground" />
    </CardHeader>
    <CardContent>
      <div className="text-2xl font-bold">{count}</div>
      <p className="text-xs text-muted-foreground">Description text</p>
    </CardContent>
  </Card>
  {/* More stat cards */}
</div>
```

### Main Content Card with Header Actions

```tsx
<Card>
  <CardHeader className="flex flex-row items-center justify-between">
    <div>
      <CardTitle>Section Title</CardTitle>
      <CardDescription>Section description</CardDescription>
    </div>
    <Button onClick={openNewModal}>
      <Plus className="h-4 w-4 mr-2" />
      Add New
    </Button>
  </CardHeader>
  <CardContent>
    {items.length === 0 ? (
      <EmptyState onAction={openNewModal} />
    ) : (
      <Table>...</Table>
    )}
  </CardContent>
</Card>
```

### Empty State Pattern

```tsx
<div className="flex flex-col items-center justify-center py-10 text-center">
  <FolderIcon className="h-12 w-12 text-muted-foreground/50 mb-4" />
  <h3 className="font-medium">No items yet</h3>
  <p className="text-sm text-muted-foreground mt-1 mb-4">
    Create your first item to get started
  </p>
  <Button onClick={onAction}>
    <Plus className="h-4 w-4 mr-2" />
    Create Item
  </Button>
</div>
```

### Table with Actions

```tsx
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
        <TableCell>
          <Badge variant={item.status === "success" ? "default" : "destructive"}>
            {item.status}
          </Badge>
        </TableCell>
        <TableCell className="text-right">
          <div className="flex justify-end gap-1">
            <Button size="icon" variant="ghost" onClick={() => handleAction(item.id)}>
              <Play className="h-4 w-4" />
            </Button>
            <Button size="icon" variant="ghost" onClick={() => openEditModal(item.id)}>
              <Pencil className="h-4 w-4" />
            </Button>
            <Button
              size="icon"
              variant="ghost"
              onClick={() => confirmDelete(item.id)}
              className="text-destructive hover:text-destructive"
            >
              <Trash2 className="h-4 w-4" />
            </Button>
          </div>
        </TableCell>
      </TableRow>
    ))}
  </TableBody>
</Table>
```

### Delete Confirmation Dialog

```tsx
<Dialog open={!!deleteId} onOpenChange={() => setDeleteId(null)}>
  <DialogContent>
    <DialogHeader>
      <DialogTitle>Delete Item</DialogTitle>
      <DialogDescription>
        Are you sure you want to delete this item? This action cannot be undone.
      </DialogDescription>
    </DialogHeader>
    <DialogFooter>
      <Button variant="outline" onClick={() => setDeleteId(null)}>
        Cancel
      </Button>
      <Button variant="destructive" onClick={handleDelete}>
        <Trash2 className="h-4 w-4 mr-2" />
        Delete
      </Button>
    </DialogFooter>
  </DialogContent>
</Dialog>
```

## Styling Guidelines

### Dark Theme

The app uses a dark theme by default with ThemeProvider.

### Status Colors

- Success: `text-emerald-500`, `bg-emerald-600`
- Error/Failed: `text-destructive`, `variant="destructive"`
- Warning: `text-amber-500`
- Primary: `text-primary`
- Muted: `text-muted-foreground`

### Loading States

```tsx
// Full page loading
if (isLoading) {
  return (
    <div className="flex items-center justify-center min-h-[50vh]">
      <Loader2 className="h-8 w-8 animate-spin text-primary" />
    </div>
  );
}

// Button loading
<Button disabled={isSaving}>
  {isSaving && <Loader2 className="h-4 w-4 mr-2 animate-spin" />}
  Save
</Button>

// Inline loading (e.g., in table row)
{isRunning ? (
  <Loader2 className="h-4 w-4 animate-spin" />
) : (
  <Play className="h-4 w-4" />
)}
```

## API Integration

### Fetch Pattern

```tsx
const fetchData = async () => {
  try {
    const response = await fetch("/api/endpoint");
    if (response.ok) {
      setData(await response.json());
    }
  } catch (error) {
    toast.error("Failed to fetch data");
  } finally {
    setIsLoading(false);
  }
};
```

### Submit Pattern

```tsx
const handleSubmit = async (e: React.FormEvent) => {
  e.preventDefault();
  setIsSaving(true);

  try {
    const url = isEditing ? `/api/items/${id}` : "/api/items";
    const method = isEditing ? "PUT" : "POST";

    const response = await fetch(url, {
      method,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(formData),
    });

    if (response.ok) {
      toast.success(isEditing ? "Updated successfully" : "Created successfully");
      onOpenChange(false);
      onSuccess();
    } else {
      const data = await response.json();
      toast.error(data.error || "Failed to save");
    }
  } catch {
    toast.error("Failed to save");
  } finally {
    setIsSaving(false);
  }
};
```

## Type Definitions

Define types in `src/types/`:

```tsx
// src/types/server.ts
export interface Server {
  id: string;
  name: string;
  host: string;
  ssh_port: number;
  ssh_user: string;
  ssh_key: string;
  created_at: string;
  updated_at: string;
}

export interface ServerFormData {
  name: string;
  host: string;
  ssh_port: number;
  ssh_user: string;
  ssh_key: string;
}
```

## Development

### Running the Dev Server

```bash
cd frontend
npm install
npm run dev
```

The dev server runs at http://localhost:5173 and proxies `/api` requests to the backend.

### Building for Production

```bash
npm run build
```

Output is in the `dist/` directory.

## Best Practices

1. **Always use shadcn/ui components** - Never create custom buttons, inputs, dialogs, etc.
2. **Use modals for create/edit** - Don't create separate pages for forms
3. **Separate resources from jobs** - S3 configs, servers are managed independently
4. **Use TypeScript types** - Define interfaces for all data structures in `src/types/`
5. **Handle loading states** - Show Loader2 spinner while fetching data
6. **Handle errors gracefully** - Use toast notifications for user feedback
7. **Group form fields with sections** - Use icons and separators to organize forms
8. **Provide empty states** - Show helpful messages and actions when lists are empty
9. **Use consistent action buttons** - Icon buttons in tables, full buttons in headers
10. **Use the `cn` helper** - For conditional class names
