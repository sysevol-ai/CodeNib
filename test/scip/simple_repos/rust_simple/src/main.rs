// SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
//
// SPDX-License-Identifier: Apache-2.0

mod math_utils;
mod shapes;

use shapes::{Circle, Rectangle, Shape};

fn test_math_utils() {
    println!("\n=== Testing Math Utils ===");

    // Test addition
    let sum = math_utils::add(5, 3);
    println!("5 + 3 = {}", sum);

    // Test multiplication
    let product = math_utils::multiply(4, 7);
    println!("4 * 7 = {}", product);

    // Test factorial
    let fact = math_utils::factorial(10);
    println!("10! = {}", fact);

    // Test prime checking
    let num = 17;
    let prime = math_utils::is_prime(num);
    println!(
        "{} is {}",
        num,
        if prime { "prime" } else { "not prime" }
    );
}

fn test_shapes() {
    println!("\n=== Testing Shapes ===");

    // Create shapes using trait objects
    let shapes: Vec<Box<dyn Shape>> = vec![
        Box::new(Rectangle::new(5.0, 3.0)),
        Box::new(Circle::new(4.0)),
    ];

    // Test polymorphism
    for shape in &shapes {
        println!("\n{}:", shape.name());
        println!("  Area: {:.2}", shape.area());
        println!("  Perimeter: {:.2}", shape.perimeter());
    }

    // Test specific methods
    let rect = Rectangle::new(10.0, 5.0);
    println!(
        "\nRectangle dimensions: {} x {}",
        rect.width(),
        rect.height()
    );

    let circle = Circle::new(7.0);
    println!("Circle radius: {}", circle.radius());
}

fn main() {
    println!("Rust SCIP Test Program");
    println!("======================");

    test_math_utils();
    test_shapes();

    println!("\n=== All tests completed ===");
}
