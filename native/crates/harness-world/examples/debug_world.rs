use harness_world::compiler::Compiler;

fn main() {
    let fixture = "The Hollow Keep\n\n## The Keeper\n\nKeeper Sarn guards the Hollow Keep. The Silver Key is hidden in the Hall of Embers. Keeper Sarn keeps the Bronze Key in the Deep Well.\n\n## The Garden of Ash\n\nThe Garden of Ash lies beyond the Iron Gate. The Deep Well stands at the center of the Garden of Ash.";
    let world = Compiler::new().compile("fixture", "The Hollow Keep", "fixture.txt", fixture);
    println!("--- seed ---");
    println!("traveller_start: {:?}", world.seed.traveller_start);
    for (loc, objs) in &world.seed.objects_by_location {
        println!("LOC {:?} -> {:?}", loc, objs);
    }
    println!("--- locations ---");
    for l in &world.locations {
        println!("{:?} ({:?})", l.name, l.id);
    }
    println!("--- object_hints via entities kind ---");
    for e in &world.entities {
        println!("{:?} kind={:?} id={:?}", e.name, e.kind, e.id);
    }
}
