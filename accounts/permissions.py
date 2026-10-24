from rest_framework import permissions

class RoleHierarchyPermission(permissions.BasePermission):
    """
    Custom permission to enforce hierarchy rules for user creation.
    """
    
    ALLOWED_CREATIONS = {
        'SUPER_ADMIN': ['SUPER_DISTRIBUTOR', 'DISTRIBUTOR', 'SUB_DISTRIBUTOR', 'RETAILER', 'CUSTOMER'],
        'STAFF_OPERATIONS': ['SUPER_DISTRIBUTOR', 'DISTRIBUTOR', 'SUB_DISTRIBUTOR', 'RETAILER', 'CUSTOMER'],
        'SUPER_DISTRIBUTOR': ['DISTRIBUTOR'],
        'DISTRIBUTOR': ['SUB_DISTRIBUTOR', 'RETAILER'],
        'SUB_DISTRIBUTOR': ['RETAILER'],
        'RETAILER': ['CUSTOMER'],
        'CUSTOMER': []
    }

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
            
        if view.action == 'create':
            creator_role = request.user.role
            target_role = getattr(view, 'target_role', None)
            
            if not target_role:
                return False
                
            return target_role in self.ALLOWED_CREATIONS.get(creator_role, [])
            
        return True
