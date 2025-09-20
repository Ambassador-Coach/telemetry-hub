"""
Dependency Injection Container for TESTRADE Application

This module provides a lightweight dependency injection container designed to solve
the instantiation nightmares experienced with the previous app.py approach:
- Eliminates circular dependencies
- Removes order-dependent instantiation
- Provides clean shutdown ordering
- Centralizes configuration management

The container uses interface-based registration and automatic dependency resolution.
"""

import inspect
import logging
from typing import Dict, Any, Type, TypeVar, Optional, List, Set, Union, get_origin, get_args
from abc import ABC, abstractmethod
from enum import Enum

logger = logging.getLogger(__name__)

T = TypeVar('T')


class LazyProxy:
    """
    A proxy object that delays the resolution of a dependency until its
    first use. This is used to break circular dependency cycles.
    """
    def __init__(self, container, interface_to_resolve):
        self._container = container
        self._interface_to_resolve = interface_to_resolve
        self._real_instance = None
        # Store interface name for better logging
        self._interface_name = getattr(self._interface_to_resolve, '__name__', str(self._interface_to_resolve))

    def _resolve_instance(self):
        """Resolves and caches the real service instance."""
        if self._real_instance is None:
            logger.debug(f"LazyProxy: Resolving real instance of {self._interface_name}")
            self._real_instance = self._container.resolve(self._interface_to_resolve)
            logger.debug(f"LazyProxy: Successfully resolved {self._interface_name}")

    def __getattr__(self, name):
        """
        Intercepts attribute access (e.g., method calls).
        Resolves the real object on first access and delegates the call.
        """
        # Avoid resolving for special attributes like `__is_proxy__`
        if name.startswith('__'):
            raise AttributeError(f"Proxy does not handle special attribute {name}")

        self._resolve_instance()
        return getattr(self._real_instance, name)


class ServiceLifecycle(Enum):
    """Service lifecycle management options."""
    SINGLETON = "singleton"  # One instance for the entire application
    TRANSIENT = "transient"  # New instance every time


class CircularDependencyError(Exception):
    """Raised when a circular dependency is detected."""
    pass


class ServiceNotRegisteredError(Exception):
    """Raised when trying to resolve an unregistered service."""
    pass


class DIContainer:
    """
    Lightweight Dependency Injection Container.
    
    Features:
    - Automatic dependency resolution via constructor inspection
    - Circular dependency detection
    - Singleton and transient lifecycle management
    - Clean shutdown ordering based on dependency graph
    - Interface-based service registration
    """
    
    def __init__(self):
        self._services: Dict[Type, tuple] = {}  # interface -> (implementation, lifecycle, factory_func)
        self._singletons: Dict[Type, Any] = {}  # interface -> instance
        self._dependency_graph: Dict[Type, Set[Type]] = {}  # interface -> dependencies
        self._resolution_stack: List[Type] = []  # For circular dependency detection
        self._shutdown_order: Optional[List[Type]] = None  # Cached shutdown order
        
    def register_singleton(self, interface: Type[T], implementation: Type[T], factory_func: Optional[callable] = None) -> 'DIContainer':
        """
        Register a service as singleton (one instance per application).
        
        Args:
            interface: The interface/abstract class
            implementation: The concrete implementation class
            factory_func: Optional factory function for complex instantiation
        """
        self._services[interface] = (implementation, ServiceLifecycle.SINGLETON, factory_func)
        self._shutdown_order = None  # Invalidate cached shutdown order
        interface_name = getattr(interface, '__name__', str(interface))
        logger.debug(f"Registered singleton: {interface_name} -> {implementation.__name__}")
        return self
        
    def register_transient(self, interface: Type[T], implementation: Type[T], factory_func: Optional[callable] = None) -> 'DIContainer':
        """
        Register a service as transient (new instance every time).
        
        Args:
            interface: The interface/abstract class
            implementation: The concrete implementation class
            factory_func: Optional factory function for complex instantiation
        """
        self._services[interface] = (implementation, ServiceLifecycle.TRANSIENT, factory_func)
        interface_name = getattr(interface, '__name__', str(interface))
        logger.debug(f"Registered transient: {interface_name} -> {implementation.__name__}")
        return self

    def register_factory(self, interface: Type[T], factory_func: callable) -> 'DIContainer':
        """
        Register a service with a factory function.

        Args:
            interface: The interface/abstract class
            factory_func: Factory function that takes DIContainer and returns instance
        """
        self._services[interface] = (None, ServiceLifecycle.SINGLETON, factory_func)
        self._shutdown_order = None  # Invalidate cached shutdown order
        interface_name = getattr(interface, '__name__', str(interface))
        logger.debug(f"Registered factory: {interface_name}")
        return self

    def register_instance(self, interface: Type[T], instance: T) -> 'DIContainer':
        """
        Register an already-created instance as singleton.
        
        Args:
            interface: The interface/abstract class
            instance: The pre-created instance
        """
        self._services[interface] = (type(instance), ServiceLifecycle.SINGLETON, None)
        self._singletons[interface] = instance
        self._shutdown_order = None  # Invalidate cached shutdown order
        interface_name = getattr(interface, '__name__', str(interface))
        logger.debug(f"Registered instance: {interface_name} -> {type(instance).__name__}")
        return self
        
    def resolve(self, interface: Type[T]) -> T:
        """
        Resolve a service instance by interface with fallback naming resolution.
        
        Args:
            interface: The interface to resolve
            
        Returns:
            The service instance
            
        Raises:
            ServiceNotRegisteredError: If the interface is not registered
            CircularDependencyError: If a circular dependency is detected
        """
        # First try exact match
        try:
            return self._resolve_exact(interface)
        except ServiceNotRegisteredError:
            pass
        
        # Then try interface name variations for naming convention flexibility
        if hasattr(interface, '__name__'):
            variations = [
                f"DI_{interface.__name__}",  # DI_ prefix
                interface.__name__,           # Raw name
                interface.__name__.replace("DI_", "")  # Without prefix
            ]
            
            for variation in variations:
                try:
                    return self._resolve_by_name(variation)
                except ServiceNotRegisteredError:
                    continue
        
        interface_name = getattr(interface, '__name__', str(interface))
        available_services = [getattr(k, '__name__', str(k)) for k in self._services.keys()]
        raise ServiceNotRegisteredError(f"Service not registered: {interface_name}. Available: {available_services}")

    def _resolve_exact(self, interface: Type[T]) -> T:
        """
        Resolve a service instance by exact interface match.
        """
        if interface not in self._services:
            interface_name = getattr(interface, '__name__', str(interface))
            raise ServiceNotRegisteredError(f"Service not registered: {interface_name}")
            
        return self._resolve_service(interface)

    def _resolve_by_name(self, service_name: str) -> T:
        """
        Resolve a service by string name (for naming convention flexibility).
        """
        # Find interface by name
        matching_interface = None
        for interface in self._services.keys():
            if getattr(interface, '__name__', str(interface)) == service_name:
                matching_interface = interface
                break
        
        if matching_interface is None:
            raise ServiceNotRegisteredError(f"Service not registered: {service_name}")
        
        return self._resolve_service(matching_interface)
    
    def _resolve_service(self, interface: Type[T]) -> T:
        """
        Internal method to resolve a service with full lifecycle management.
        """
        implementation, lifecycle, factory_func = self._services[interface]
        
        # Return existing singleton if available
        if lifecycle == ServiceLifecycle.SINGLETON and interface in self._singletons:
            return self._singletons[interface]
            
        # Check for circular dependencies
        if interface in self._resolution_stack:
            cycle = " -> ".join([getattr(cls, '__name__', str(cls)) for cls in self._resolution_stack[self._resolution_stack.index(interface):]])
            interface_name = getattr(interface, '__name__', str(interface))
            raise CircularDependencyError(f"Circular dependency detected: {cycle} -> {interface_name}")
            
        self._resolution_stack.append(interface)
        
        try:
            # Create the instance
            if factory_func:
                instance = factory_func(self)
            else:
                instance = self._create_instance(implementation)
                
            # Store singleton
            if lifecycle == ServiceLifecycle.SINGLETON:
                self._singletons[interface] = instance
                
            return instance
            
        finally:
            self._resolution_stack.pop()
            
    def _create_instance(self, implementation: Type) -> Any:
        """
        Create an instance of the implementation class with automatic dependency injection.

        Args:
            implementation: The class to instantiate

        Returns:
            The created instance with dependencies injected
        """
        # Get constructor signature
        signature = inspect.signature(implementation.__init__)
        parameters = signature.parameters

        # Skip 'self' parameter
        param_names = [name for name in parameters.keys() if name != 'self']

        # Resolve dependencies
        dependencies = {}
        service_dependencies = set()

        for param_name in param_names:
            param = parameters[param_name]
            annotated_type = param.annotation

            # Skip parameters without type annotations
            if annotated_type == inspect.Parameter.empty:
                if param.default != inspect.Parameter.empty:
                    dependencies[param_name] = param.default
                    logger.debug(f"Parameter {param_name} for {implementation.__name__} using default value (no type hint).")
                else:
                    logger.warning(f"Parameter {param_name} for {implementation.__name__} has no type hint and no default. Cannot inject.")
                continue

            # Handle Optional[X] which is Union[X, NoneType]
            is_optional = False
            actual_type_to_resolve = annotated_type

            origin = get_origin(annotated_type)
            if origin is Union:
                args = get_args(annotated_type)
                if len(args) == 2 and type(None) in args:
                    is_optional = True
                    actual_type_to_resolve = args[0] if args[1] is type(None) else args[1]
                    type_name = getattr(actual_type_to_resolve, '__name__', str(actual_type_to_resolve))
                    logger.debug(f"Detected Optional[{type_name}] for {param_name} in {implementation.__name__}")

            # Skip basic types unless they're registered services
            if actual_type_to_resolve in (str, int, float, bool, dict, list, set) and actual_type_to_resolve not in self._services:
                if param.default != inspect.Parameter.empty:
                    dependencies[param_name] = param.default
                elif is_optional:
                    dependencies[param_name] = None
                continue

            # Try to resolve the dependency
            if actual_type_to_resolve in self._services:
                try:
                    resolved_instance = self.resolve(actual_type_to_resolve)
                    dependencies[param_name] = resolved_instance
                    service_dependencies.add(actual_type_to_resolve)
                    type_name = getattr(actual_type_to_resolve, '__name__', str(actual_type_to_resolve))
                    logger.debug(f"Successfully resolved dependency {param_name}: {type_name} for {implementation.__name__}")
                except CircularDependencyError:
                    # Re-raise circular dependency errors immediately
                    raise
                except Exception as e:
                    type_name = getattr(actual_type_to_resolve, '__name__', str(actual_type_to_resolve))
                    logger.warning(f"Failed to resolve dependency {param_name}: {type_name} for {implementation.__name__}: {e}")
                    if is_optional:
                        dependencies[param_name] = None
                        logger.debug(f"Optional dependency {param_name} failed to resolve, injecting None")
                    elif param.default != inspect.Parameter.empty:
                        dependencies[param_name] = param.default
                        logger.debug(f"Using default value for {param_name}")
                    else:
                        # No default value, this is a critical dependency failure
                        raise
            elif param.default != inspect.Parameter.empty:
                # Use default value if available
                dependencies[param_name] = param.default
                logger.debug(f"Using default value for unregistered dependency {param_name}")
            elif is_optional:
                # Optional dependency not registered - inject None
                dependencies[param_name] = None
                type_name = getattr(actual_type_to_resolve, '__name__', str(actual_type_to_resolve))
                logger.debug(f"Optional dependency {param_name}: {type_name} not registered, injecting None")
            else:
                type_name = getattr(actual_type_to_resolve, '__name__', str(actual_type_to_resolve))
                logger.warning(f"Cannot resolve required dependency {param_name}: {type_name} for {implementation.__name__}")

        # Track dependencies for shutdown ordering
        interface = self._find_interface_for_implementation(implementation)
        if interface:
            self._dependency_graph[interface] = service_dependencies

        # Create instance with resolved dependencies
        logger.debug(f"Creating {implementation.__name__} with dependencies: {list(dependencies.keys())}")
        return implementation(**dependencies)
        
    def _find_interface_for_implementation(self, implementation: Type) -> Optional[Type]:
        """Find the interface that maps to this implementation."""
        for interface, (impl, _, _) in self._services.items():
            if impl == implementation:
                return interface
        return None
        
    def get_shutdown_order(self) -> List[Type]:
        """
        Get the proper shutdown order based on dependency graph.
        Services with no dependencies shut down first, then their dependents.
        
        Returns:
            List of interfaces in shutdown order
        """
        if self._shutdown_order is not None:
            return self._shutdown_order
            
        # Topological sort for shutdown order (reverse of startup order)
        visited = set()
        temp_visited = set()
        shutdown_order = []
        
        def visit(interface: Type):
            if interface in temp_visited:
                interface_name = getattr(interface, '__name__', str(interface))
                raise CircularDependencyError(f"Circular dependency detected involving {interface_name}")
            if interface in visited:
                return
                
            temp_visited.add(interface)
            
            # Visit dependencies first
            for dependency in self._dependency_graph.get(interface, set()):
                visit(dependency)
                
            temp_visited.remove(interface)
            visited.add(interface)
            shutdown_order.append(interface)
            
        # Visit all registered services
        for interface in self._services.keys():
            if interface not in visited:
                visit(interface)
                
        # Reverse for shutdown order (dependencies shut down after dependents)
        self._shutdown_order = list(reversed(shutdown_order))
        return self._shutdown_order
        
    def shutdown_all(self):
        """
        Shutdown all singleton services in proper dependency order.
        Services that depend on others are shut down first.
        """
        shutdown_order = self.get_shutdown_order()
        
        for interface in shutdown_order:
            if interface in self._singletons:
                instance = self._singletons[interface]
                
                # Call shutdown method if available
                if hasattr(instance, 'stop'):
                    try:
                        interface_name = getattr(interface, '__name__', str(interface))
                        logger.info(f"Shutting down service: {interface_name}")
                        instance.stop()
                    except Exception as e:
                        interface_name = getattr(interface, '__name__', str(interface))
                        logger.error(f"Error shutting down {interface_name}: {e}")
                        
        # Clear singletons
        self._singletons.clear()
        logger.info("All services shut down successfully")
        
    def get_dependency_info(self) -> Dict[str, Any]:
        """
        Get diagnostic information about registered services and dependencies.
        
        Returns:
            Dictionary with service registration and dependency information
        """
        info = {
            'registered_services': {},
            'dependency_graph': {},
            'singletons_created': list(self._singletons.keys()),
            'shutdown_order': [getattr(cls, '__name__', str(cls)) for cls in self.get_shutdown_order()]
        }
        
        for interface, (implementation, lifecycle, factory_func) in self._services.items():
            interface_name = getattr(interface, '__name__', str(interface))
            info['registered_services'][interface_name] = {
                'implementation': implementation.__name__ if implementation else 'Factory',
                'lifecycle': lifecycle.value,
                'has_factory': factory_func is not None
            }
            
        for interface, dependencies in self._dependency_graph.items():
            interface_name = getattr(interface, '__name__', str(interface))
            info['dependency_graph'][interface_name] = [getattr(dep, '__name__', str(dep)) for dep in dependencies]
            
        return info
    
    def get_all_instances_of_type(self, interface: Type[T]) -> List[T]:
        """
        Gets all singleton instances that are a subclass of a given type.
        
        Args:
            interface: The interface/type to match against
            
        Returns:
            List of instances that are subclasses of the given type
        """
        instances = []
        for instance in self._singletons.values():
            if isinstance(instance, interface):
                instances.append(instance)
        return instances
    
    def get_all_singleton_instances(self) -> Dict[Type, Any]:
        """
        Get all singleton instances mapped by their interface.
        
        Returns:
            Dictionary mapping interface to instance
        """
        return self._singletons.copy()
    
    def get_registered_services(self) -> List[str]:
        """
        Get list of all registered service names for diagnostics.
        
        Returns:
            List of service names (interface names)
        """
        service_names = []
        for interface in self._services.keys():
            service_name = getattr(interface, '__name__', str(interface))
            service_names.append(service_name)
        return sorted(service_names)
    
    def get_registration_diagnostics(self) -> Dict[str, Any]:
        """
        Get comprehensive diagnostics about service registration.
        
        Returns:
            Dictionary with registration statistics and details
        """
        total_registered = len(self._services)
        total_singletons = len(self._singletons)
        
        service_details = []
        for interface, (implementation, lifecycle, factory_func) in self._services.items():
            interface_name = getattr(interface, '__name__', str(interface))
            impl_name = getattr(implementation, '__name__', str(implementation)) if implementation else 'Factory'
            is_instantiated = interface in self._singletons
            
            service_details.append({
                'interface': interface_name,
                'implementation': impl_name,
                'lifecycle': lifecycle,
                'has_factory': factory_func is not None,
                'is_instantiated': is_instantiated
            })
        
        return {
            'total_registered': total_registered,
            'total_instantiated': total_singletons,
            'service_details': service_details
        }
